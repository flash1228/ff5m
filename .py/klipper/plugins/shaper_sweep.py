# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# Copyright (C) 2020-2024, Dmitry Butyugin <dmbutyugin@google.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

"""Klipper 0.13 sweeping vibrations on the FF5M CoreXY toolhead."""

import math
from types import SimpleNamespace


class SweepingVibrations:
    # Amplitude of the slow upstream sweep overlaid on the test pulses.
    SWEEP_ACCEL = 400.

    def __init__(self, test, printer_settings, gcmd, safe_z, accel_per_hz):
        self.freq_start, self.freq_end = test.freq_start, test.freq_end
        self.hz_per_sec, self.accel_per_hz = test.hz_per_sec, accel_per_hz
        # The trapezoids bypass the lookahead, so the runtime M204 or
        # SET_VELOCITY_LIMIT value left by the last print does not apply.
        # Check against the machine's configured [printer] limits instead,
        # without changing the runtime ones.
        self.max_velocity = printer_settings.get("max_velocity", 0.)
        self.max_accel = printer_settings.get("max_accel", 0.)
        self.keep_shaping = gcmd.get_int("INPUT_SHAPING", 0)
        self.safe_z = safe_z
        if printer_settings.get("kinematics") != "corexy":
            raise gcmd.error("Sweeping vibrations require CoreXY kinematics")
        # prepare_test checks the FREQ_* range, but NaN passes its comparisons.
        for name, value in (("FREQ_START", self.freq_start), ("FREQ_END", self.freq_end)):
            if not math.isfinite(value):
                raise gcmd.error("%s must be a finite number" % name)
        if not .1 <= self.hz_per_sec <= 2.:
            raise gcmd.error("HZ_PER_SEC must be 0.1 to 2 for sweeping vibrations")
        if not (math.isfinite(self.accel_per_hz) and self.accel_per_hz > 0.):
            raise gcmd.error("accel_per_hz must be a positive number for sweeping vibrations")
        if not all(math.isfinite(value) and value > 0. for value in (self.max_velocity, self.max_accel)):
            raise gcmd.error("Invalid configured velocity or acceleration limit")

    def _accelerations(self):
        # Klipper v0.13.0 resonance_tester.py, SweepingVibrationsTestGenerator.
        # Keep the upstream phase and overlay the sweep on Forge-X's pulse settings.
        period, sweep_accel = 1.2, self.SWEEP_ACCEL
        first_fraction = math.sqrt(2.) / 8.
        remaining = period * first_fraction
        next_sweep_duration = period * (first_fraction + .25)
        freq, sign = self.freq_start, 1.
        while freq <= self.freq_end + .000001:
            quarter = .25 / freq
            for pulse in (sign, -sign):
                duration = quarter
                accel = pulse * self.accel_per_hz * freq
                while remaining <= duration:
                    yield remaining, accel + sweep_accel, freq
                    duration -= remaining
                    remaining, next_sweep_duration = next_sweep_duration, period / 2.
                    sweep_accel = -sweep_accel
                if duration:
                    yield duration, accel + sweep_accel, freq
                    remaining -= duration
            freq += 2. * quarter * self.hz_per_sec
            sign = -sign

    def _segments(self):
        velocity = distance = 0.
        for duration, accel, freq in self._accelerations():
            next_velocity = velocity + accel * duration
            if velocity * next_velocity < 0.:
                # Each trapezoid travels in one direction. Include the turning
                # point in the clearance check, even inside an acceleration pulse.
                stop_time = -velocity / accel
                distance += .5 * velocity * stop_time
                yield distance, stop_time, velocity, 0., accel, freq
                duration -= stop_time
                velocity = 0.
            distance += .5 * (velocity + next_velocity) * duration
            yield distance, duration, velocity, next_velocity, accel, freq
            velocity = next_velocity
        if velocity:
            # Brake no harder than the sweep's own peak acceleration.
            brake = self.accel_per_hz * self.freq_end + self.SWEEP_ACCEL
            stop_time = abs(velocity) / brake
            distance += .5 * velocity * stop_time
            yield distance, stop_time, velocity, 0., -math.copysign(brake, velocity), self.freq_end

    def validate(self, point, axes, status, command, check):
        if not all(axis in status["homed_axes"] for axis in "xyz"):
            raise command.error("Home all axes before sweeping vibrations")
        if not 0. < self.safe_z <= point[2]:
            raise command.error("The probe point must be at or above Safe Z for sweeping vibrations")
        minimum, maximum = status["axis_minimum"], status["axis_maximum"]
        low = high = 0.
        for index, (distance, _, v0, v1, accel, _) in enumerate(self._segments()):
            if abs(accel) > self.max_accel or max(abs(v0), abs(v1)) > self.max_velocity:
                raise command.error("Sweeping vibrations exceed the configured velocity or acceleration limit")
            low, high = min(low, distance), max(high, distance)
            if index % 4096 == 0:
                check()
        positions = [tuple(point)]
        for axis in axes:
            for offset in (low, high):
                dx, dy = axis.get_point(offset)
                positions.append((point[0] + dx, point[1] + dy, point[2]))
        if any(not all(lo <= pos <= hi for lo, pos, hi in zip(minimum, xyz, maximum))
               for xyz in positions):
            raise command.error("Not enough travel around the probe point for sweeping vibrations")

    def run(self, toolhead, axis, command, check):
        printer = toolhead.printer
        shaper = printer.lookup_object("input_shaper", None) if not self.keep_shaping else None
        try:
            if shaper is not None:
                shaper.disable_shaping()
            window = self._queue(toolhead, axis, command, check)
        finally:
            try:
                toolhead.wait_moves()
                printer.send_event("toolhead:manual_move")
            finally:
                if shaper is not None:
                    shaper.enable_shaping()
        if printer.is_shutdown():
            raise command.error("Printer shut down during sweeping vibrations")
        return window

    def _queue(self, toolhead, axis, command, check):
        # The old lookahead clips junction speeds even on collinear short moves.
        # Feed the exact XY trapezoids to its normal trapq and step-generation path.
        origin = previous = toolhead.get_position()
        batch, duration = [], 0.
        start = end = None
        last_freq = -1

        def submit(position):
            nonlocal start
            first = start is None
            if first:
                # Resynchronize print_time now, as the first _process_moves
                # would, so the window starts with the first queued trapezoid.
                start = toolhead.get_last_move_time()
            return self._submit(toolhead, batch, position, not first, command, check)

        for distance, dt, v0, v1, accel, freq in self._segments():
            dx, dy = axis.get_point(distance)
            position = [origin[0] + dx, origin[1] + dy, origin[2], origin[3]]
            vx, vy = axis.get_point(math.copysign(1., v1 or v0))
            speed0, speed1 = abs(v0), abs(v1)
            batch.append(SimpleNamespace(
                is_kinematic_move=bool(speed0 or speed1), start_pos=previous,
                axes_d=(0., 0., 0., 0.), axes_r=(vx, vy, 0.),
                start_v=speed0, cruise_v=max(speed0, speed1), accel=abs(accel),
                accel_t=dt if speed0 < speed1 else 0.,
                cruise_t=dt if speed0 == speed1 else 0.,
                decel_t=dt if speed0 > speed1 else 0., timing_callbacks=()))
            previous = position
            duration += dt
            # Only yield to cancellation with a complete stop already queued.
            if v1 == 0. and duration >= .125:
                if math.floor(freq) > last_freq:
                    command.respond_info("Testing frequency %.0f Hz" % freq)
                    last_freq = math.floor(freq)
                end = submit(position)
                batch, duration = [], 0.
        if batch:
            end = submit(previous)
        return start, end

    @staticmethod
    def _submit(toolhead, batch, position, continuing, command, check):
        check()
        if toolhead.printer.is_shutdown():
            raise command.error("Printer shut down during sweeping vibrations")
        # After the first batch, each one must start where the previous ended.
        estimated = toolhead.mcu.estimated_print_time(toolhead.reactor.monotonic())
        if continuing and (toolhead.special_queuing_state or toolhead.print_time <= estimated):
            raise command.error("Motion buffer ran dry during sweeping vibrations")
        toolhead.commanded_pos[:] = position
        toolhead._process_moves(batch)
        actual_end = toolhead.print_time
        # Batches end only at rests, about every 0.6 s of motion. Resume
        # queueing with 1 s above the low-buffer flush mark instead of
        # buffer_time_high, so a reactor delay shorter than that cannot let
        # the flush timer end the sweep. Until that margin is primed, queue
        # the next batch without yielding.
        resume = toolhead.buffer_time_low + 1.
        eventtime = toolhead.reactor.monotonic()
        while True:
            excess = actual_end - toolhead.mcu.estimated_print_time(eventtime) - resume
            if excess <= 0.:
                break
            eventtime = toolhead.reactor.pause(eventtime + min(1., excess))
        return actual_end
