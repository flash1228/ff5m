# Feather screen guide

Feather is the default touchscreen interface of Forge-X. To switch from another display mode, see [Screens](SCREEN.md#switching-commands).

## Getting started

Press **MENU** on the home screen to open **PRINT FILES**, **CONTROL**, **FILAMENT**, or **NETWORK**. Settings and calibration are under **CONTROL**.

The first touch after the panel dims only wakes the display. Touch again to use a control.

## Starting a print

1. Open **MENU → PRINT FILES** and select a G-code file from the printer or a connected USB drive. Use refresh if a newly uploaded file is missing.
2. On the confirmation screen, select **PREVIEW** to inspect the model. Enable **REBUILD BED MESH** if you want a fresh full-bed mesh for this print; this also replaces adaptive meshing when KAMP is enabled.
3. Select **START PRINT** on the confirmation screen or **PRINT** in the model preview.

Back from the model preview returns to the file browser. To return to print confirmation instead, disable **Preview back to files** under **CONTROL → SETTINGS → MOD PARAMETERS → USABILITY**.

If you select **SAVE MESH FOR FUTURE PRINTS**, Feather asks you to confirm after the print succeeds. Confirming saves the mesh permanently and restarts Klipper. Postponing leaves it available only for the current session.

For file uploads and slicer thumbnails, see [Slicing](SLICING.md).

## During a print

Use the print screen to pause, resume, or cancel. Select **MORE → Change Filament** to change material, or **Z ADJUST** to adjust nozzle height. Live Z changes take effect immediately; press **SAVE** to keep the offset after a restart. See [Z offset](PRINTING.md#z-offset).

Cancellation during preparation may wait for the current movement, homing, or probing step to finish. Heating waits can stop immediately. While cancellation is pending, **CONTINUE OPERATION** withdraws the request if it has not taken effect. **ABORT NOW** (`M112`) stops the printer immediately. If a step cannot be cancelled safely, only Continue or emergency stop is available.

If a previous timelapse is still being saved, choose **WAIT**, **CANCEL PRINT**, or **CANCEL TIMELAPSE** before starting the next print. See [Timelapse](CAMERA.md#timelapse).

## Printer controls and maintenance

Start from **MENU**:

| Task | Where to go |
| --- | --- |
| Home or move the printer | **CONTROL → MOVE**; home the required axes before moving. |
| Heat, preheat, cool down, or adjust the part fan | **CONTROL → HEAT / FAN**. |
| Load, unload, or change filament | **FILAMENT**; select the material and follow the heating instructions. |
| Calibrate the printer | **CONTROL → CALIBRATION**; follow the instructions and review the result before saving. |

Read [Calibration](CALIBRATION.md#before-you-start) before changing the mesh or Z offset. For filament procedures, see [Filament management](MACROS.md#filament-management).

## Connecting to the network

Open **MENU → NETWORK**. Select **WI-FI**, choose a network, and enter its password, or connect a cable and select **ETHERNET DHCP**. Use the IP address shown on the Network page to open Fluidd or Mainsail.

Selecting a saved Wi-Fi network reconnects without asking for its password. Use **RESET PASSWORD** if the password has changed.

If the screen shows **CONNECTING**, you can wait or cancel the connection from the Network page before choosing another network. Feather remains usable offline. Changing the network briefly interrupts remote access; if a Wi-Fi change fails, Feather tries to restore the previous saved network.

## Settings and updates

Open **MENU → CONTROL → SETTINGS** for screen brightness, chamber light, sound, and **COLOR THEME**. Select **MOD PARAMETERS** for Forge-X options. Check any restart requirement shown before applying a change. See [Configuration](CONFIGURATION.md#changing-forge-x-settings-from-feather) for more details.

When an update notification appears while the printer is idle, select **UPDATE** to install it or **LATER** to dismiss that version until the printer or Klipper restarts.

## Further help and limits

Use Fluidd or Mainsail for file deletion, unrestricted G-code, and detailed diagnostics. Static IP addresses and enterprise Wi-Fi require configuration outside Feather.

For recovery after an interrupted print, see [Power Loss Recovery](POWER_LOSS_RECOVERY.md). For changing display modes and the bed-mesh profile used by each mode, see [Screens](SCREEN.md#switching-to-alternative-screens--headless).
