## Memory attribution of the Klipper process for on-printer UI diagnostics.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import collections
import gc
import json
import os
import re
import sys
import tempfile
import time
import types

from .artifacts import ARTIFACT_ROOT

MAPPING = re.compile(r"^[0-9a-f]+-[0-9a-f]+ \S+ \S+ \S+ \d+\s*(.*)$")
LARGEST_MAPPINGS = 12
TOP_TYPES = 25


def summarize_smaps(text):
    """Group /proc/<pid>/smaps by what the mapping is, in kB: the main heap, stacks, other named areas, files and
    the unnamed anonymous mappings (Python's arenas, other threads' malloc arenas, thread stacks), the largest of
    which are listed so that the groups can be told apart. The files' own anonymous pages (relocations, .data and
    .bss of the engine module and the libraries) are listed by file."""
    groups = {"heap": [0, 0, 0], "stack": [0, 0, 0], "special": [0, 0, 0], "file": [0, 0, 0],
              "anonymous": [0, 0, 0]}
    anonymous = []
    files = {}
    current = None

    def finish():
        if current is None:
            return
        name, size, rss, anon = current["name"], current["Size"], current["Rss"], current["Anonymous"]
        if name == "[heap]":
            group = "heap"
        elif name.startswith("[stack"):
            group = "stack"
        elif name.startswith("["):
            group = "special"
        elif name:
            group = "file"
            if anon:
                files[name] = files.get(name, 0) + anon
        else:
            group = "anonymous"
            anonymous.append({"size": size, "rss": rss, "anonymous": anon})
        for index, value in enumerate((size, rss, anon)):
            groups[group][index] += value

    for line in text.splitlines():
        header = MAPPING.match(line)
        if header:
            finish()
            current = {"name": header.group(1).strip(), "Size": 0, "Rss": 0, "Anonymous": 0}
            continue
        field, _, rest = line.partition(":")
        if current is not None and field in ("Size", "Rss", "Anonymous"):
            current[field] = int(rest.split()[0])
    finish()
    anonymous.sort(key=lambda mapping: mapping["rss"], reverse=True)
    result = {name: dict(zip(("size", "rss", "anonymous"), values)) for name, values in groups.items()}
    result["anonymous"]["count"] = len(anonymous)
    result["anonymous"]["largest"] = anonymous[:LARGEST_MAPPINGS]
    result["file_anonymous"] = dict(sorted(files.items(), key=lambda item: item[1], reverse=True)[
        :LARGEST_MAPPINGS])
    return result


def count_types(objects):
    """The number of tracked objects by type name, the most numerous first."""
    counts = collections.Counter(type(item).__name__ for item in objects)
    return {"total": sum(counts.values()), "by_type": dict(counts.most_common(TOP_TYPES))}


def read_text(path):
    with open(path, "r") as source:
        return source.read()


def process_state(read=None, path="/proc/self/status"):
    """A process's RSS split, in kB: Klipper's own unless path names another's status."""
    read = read or read_text
    state = {}
    for line in read(path).splitlines():
        field, _, rest = line.partition(":")
        if field in ("VmRSS", "RssAnon", "RssFile", "VmHWM", "VmSwap"):
            state[field] = int(rest.split()[0])
    return state


def snapshot(malloc, read=None):
    """The process split and its mappings, with glibc's main-arena figures in bytes (in use, free but held, mapped
    directly) when the engine can read them; a thread's own arena is not in those, the smaps groups cover it."""
    read = read or read_text
    return {"process": process_state(read), "smaps": summarize_smaps(read("/proc/self/smaps")), "malloc": malloc}


def python_state(engine):
    """What Python allocates for an empty container on this interpreter, what its collector tracks, and of that the
    engine's node proxies. Unreachable cycles are collected first, so the counts are what the process keeps."""
    result = {"version": sys.version.split()[0], "empty_dict": sys.getsizeof({}), "empty_set": sys.getsizeof(set())}
    # Before the collect below: how often each generation was collected since start (whether a full collection
    # ever runs after gc.freeze()), the pending counts and how many objects the freeze keeps out of collection.
    result["gc"] = {"stats": gc.get_stats(), "count": list(gc.get_count()), "frozen": gc.get_freeze_count()}
    result["collected"] = gc.collect()
    objects = gc.get_objects()
    result["objects"] = count_types(objects)
    result["node_proxies"] = count_types([item for item in objects if isinstance(item, engine.NodeBase)])
    return result


def parse_pymalloc_stats(text):
    """sys._debugmallocstats' text as its totals (name: bytes or count; "arenas" is the bytes of the arenas held, an
    "N unused pools" line is named "unused pools") and its size classes as [block size, pools, blocks in use,
    blocks available]."""
    totals, classes = {}, []
    for line in text.splitlines():
        row = line.split()
        if len(row) == 5 and all(part.isdigit() for part in row):
            classes.append([int(part) for part in row[1:]])
            continue
        left, equals, right = line.rpartition("=")
        value = right.strip().replace(",", "")
        if equals and value.isdigit():
            name = re.sub(r"^\d+\s+", "", left.split("*")[0].strip().lstrip("#").strip())
            totals[name] = int(value)
    return {"totals": totals, "classes": classes}


def pymalloc_state():
    """How full pymalloc's arenas are: the blocks Python's small objects use against what the arenas hold. None where
    the interpreter does not use pymalloc (PYTHONMALLOC=malloc) and so prints nothing. The statistics go to C's
    stderr, so descriptor 2 points at a file for the call and is put back whatever happens."""
    stats = getattr(sys, "_debugmallocstats", None)
    if stats is None:
        return None
    sys.stderr.flush()
    with tempfile.TemporaryFile() as sink:
        saved = os.dup(2)
        try:
            os.dup2(sink.fileno(), 2)
            stats()
        finally:
            os.dup2(saved, 2)
            os.close(saved)
        sink.seek(0)
        state = parse_pymalloc_stats(sink.read().decode("ascii", "replace"))
    return state if state["totals"] else None


def typer_pid(proc="/proc", pid=None):
    """The pid of the Typer process this Klipper started (its child named typer), or None."""
    pid = os.getpid() if pid is None else pid
    try:
        entries = os.listdir(proc)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "stat"), "r") as source:
                stat = source.read()
        except OSError:
            continue
        name, rest = stat[stat.find("(") + 1:stat.rfind(")")], stat[stat.rfind(")") + 1:].split()
        if name == "typer" and rest[1:2] == [str(pid)]:
            return int(entry)
    return None


def descriptor_counts(proc="/proc", pid=None):
    """Open file descriptors of this process (Klipper) and of the Typer process it started; None where /proc
    cannot tell."""
    pid = os.getpid() if pid is None else pid

    def count(path):
        try:
            return len(os.listdir(path))
        except OSError:
            return None

    typer = typer_pid(proc, pid)
    return {"klipper": count(os.path.join(proc, str(pid), "fd")),
            "typer": None if typer is None else count(os.path.join(proc, str(typer), "fd"))}


# What a leak check compares: counts of things the UI keeps, exact after a collection, unlike RSS. Klipper's own
# descriptors are recorded beside them but not compared: they move with the connections Klipper serves (36 -> 35
# over one LEAK run on the printer), while Typer's stay fixed.
LEAK_COUNTERS = ("nodes", "images", "mirror_nodes", "node_proxies", "shared_defaults", "call_shapes", "typer_fds")


def leak_counters(engine=None, proc="/proc"):
    """The LEAK_COUNTERS and Klipper's descriptors now, after unreachable cycles are collected (their nodes would be
    counted otherwise)."""
    engine = engine or _engine()
    from ui.layout import call_shapes_count, shared_defaults_count
    # A released proxy can release nodes whose own cycles only the next pass finds; a few passes reach nothing left.
    for _ in range(10):
        if not gc.collect():
            break
    census, descriptors = engine.census(), descriptor_counts(proc)
    return {"nodes": census["nodes"], "images": census["images"], "mirror_nodes": census["mirror_nodes"],
            "node_proxies": sum(1 for item in gc.get_objects() if isinstance(item, engine.NodeBase)),
            "shared_defaults": shared_defaults_count(), "call_shapes": call_shapes_count(),
            "klipper_fds": descriptors["klipper"], "typer_fds": descriptors["typer"]}


def leak_growth(before, after):
    """The counters that grew from before to after, as name: [before, after]; one unknown on either side is not
    compared."""
    return {name: [before[name], after[name]] for name in LEAK_COUNTERS
            if before[name] is not None and after[name] is not None and after[name] > before[name]}


# Owners of the Python heap, in the order they claim what they reach: an object two of them reach is counted once,
# for the first. The node proxies come first, so what a node holds is the UI's however a module reaches it.
FEATHER_FRAMEWORK, FEATHER_PRODUCT, FEATHER_UI_TEST = "feather framework", "feather product", "feather ui test"
KLIPPER, PYTHON = "klipper and mod", "python and other"
# Python's modules that only Feather's modules reach: what Klipper would not have imported without Feather.
PYTHON_FOR_FEATHER = "python for feather"
FEATHER_OWNERS = (FEATHER_FRAMEWORK, FEATHER_PRODUCT, FEATHER_UI_TEST)
OWNERS = FEATHER_OWNERS + (KLIPPER, PYTHON_FOR_FEATHER, PYTHON)
FEATHER_PRODUCT_MODULES = frozenset(("feather", "feather_screen", "ff5m_ui"))
# Objects that are a program's code rather than its data: classes, functions and what they are made of.
CODE_TYPES = (type, types.FunctionType, types.CodeType, types.MethodType, property, classmethod, staticmethod,
              types.BuiltinFunctionType, type(type.__dict__["mro"]), type(type.__dict__["__dict__"]),
              type(object.__init__), type(types.FunctionType.__globals__))


def owner_of(module_name):
    """Who a module belongs to, by its name: Klipper loads plugins as extras.<name>."""
    name = module_name or ""
    if name.startswith("extras."):
        name = name[len("extras."):]
    top = name.split(".")[0]
    # The engine's own types (a node's LayoutOptions, class records) name their module feather_engine wherever the
    # module is imported from: ui.feather_engine on the printer.
    if top in ("ui", "feather_engine"):
        return FEATHER_FRAMEWORK
    if top in FEATHER_PRODUCT_MODULES:
        return FEATHER_PRODUCT
    if top == "feather_ui_test":
        return FEATHER_UI_TEST
    if module_name in sys.builtin_module_names or not name:
        return PYTHON
    module = sys.modules.get(module_name)
    path = getattr(module, "__file__", None) or ""
    return KLIPPER if "/klippy/" in path or module_name.startswith("extras.") or "/plugins/" in path else PYTHON


def modules_reached(modules, owner):
    """Python's modules (owner PYTHON) by who reaches them through module globals (a module, or a class or function
    taken from one) and a submodule's package: {"feather only": [...], "unreached": [...]}. A module that Klipper
    reaches is not Feather's whatever else reaches it. One imported inside a function only is reached by no global
    and is listed as unreached, as are those the interpreter imports at startup."""
    def references(name):
        found = set([name.rpartition(".")[0]]) if "." in name else set()
        for value in list(vars(modules[name]).values()):
            if isinstance(value, types.ModuleType):
                found.add(value.__name__)
            elif isinstance(value, (type, types.FunctionType, types.BuiltinFunctionType)):
                found.add(getattr(value, "__module__", None))
        return [other for other in found if other in modules and other != name and owner(other) == PYTHON]

    def reached(roots):
        seen, stack = set(), list(roots)
        while stack:
            name = stack.pop()
            if name not in seen:
                seen.add(name)
                stack.extend(references(name))
        return seen

    named = [name for name, module in modules.items() if module is not None]
    klipper = reached(name for name in named if owner(name) == KLIPPER)
    feather = reached(name for name in named if owner(name) in FEATHER_OWNERS)
    python = set(name for name in named if owner(name) == PYTHON)
    return {"feather only": sorted((feather - klipper) & python), "unreached": sorted(python - feather - klipper)}


def attribution(engine=None, modules=None, owner=None):
    """sys.getsizeof of the Python heap by owner, in bytes and objects: the node proxies with what only they hold,
    then each owner's code (classes, functions) and data (module values and its classes' instances). It unfreezes the
    startup heap (gc.freeze at klippy:ready) so its objects can be found; that lasts until the next restart and only
    makes the collector scan them. Native memory is not here: the census and malloc figures cover it."""
    engine = engine or _engine()
    modules = dict(sys.modules) if modules is None else modules
    owner = owner or owner_of
    reach = modules_reached(modules, owner)
    feather_only, by_name = set(reach["feather only"]), owner
    owner = lambda name: PYTHON_FOR_FEATHER if name in feather_only else by_name(name)
    started = time.monotonic()
    if hasattr(gc, "unfreeze"):
        gc.unfreeze()
    for _ in range(10):
        if not gc.collect():
            break
    objects = gc.get_objects()
    module_ids = set(id(module) for module in modules.values() if module is not None)
    # A function reaches its module's namespace (__globals__); that namespace is the module's data, not code.
    namespaces = set(id(vars(module)) for module in modules.values() if module is not None)
    seen = set()
    totals = {}

    def owned_by(item):
        """The owner a class, function or instance declares through its module; None for builtin containers."""
        if isinstance(item, (type, types.FunctionType)):
            return owner(getattr(item, "__module__", None))
        module = type(item).__module__
        return None if module == "builtins" else owner(module)

    def walk(roots, bucket, stop):
        stack = list(roots)
        roots = set(id(item) for item in roots)
        while stack:
            item = stack.pop()
            if id(item) in seen or (id(item) not in roots and stop(item)):
                continue
            seen.add(id(item))
            entry = totals.setdefault(bucket, [0, 0])
            entry[0] += 1
            entry[1] += sys.getsizeof(item)
            stack.extend(gc.get_referents(item))

    seen.update((id(objects), id(seen), id(totals), id(modules)))
    proxies = [item for item in objects if isinstance(item, engine.NodeBase)]
    walk(proxies, "nodes", lambda item: (id(item) in module_ids or isinstance(item, (engine.NodeBase,) + CODE_TYPES)
                                         or owned_by(item) not in (None, FEATHER_FRAMEWORK)))
    by_owner = {name: [module for module_name, module in modules.items()
                       if module is not None and owner(module_name) == name] for name in OWNERS}
    for name in OWNERS:
        stranger = lambda item, name=name: id(item) in module_ids or owned_by(item) not in (None, name)
        code = [value for module in by_owner[name] for value in vars(module).values()
                if isinstance(value, (type, types.FunctionType)) and owned_by(value) == name]
        walk(code, (name, "code"), lambda item, stranger=stranger: stranger(item) or id(item) in namespaces or (
            not isinstance(item, CODE_TYPES) and type(item).__module__ != "builtins"))
        instances = [item for item in objects if not isinstance(item, CODE_TYPES) and owned_by(item) == name]
        walk(by_owner[name] + instances, (name, "data"), stranger)
    left = [item for item in objects if id(item) not in seen]
    result = {"nodes": totals.get("nodes", [0, 0]), "proxies": len(proxies),
              "unreached": [len(left), sum(sys.getsizeof(item) for item in left)], "python modules": reach}
    result["seconds"] = round(time.monotonic() - started, 3)  # the reactor waits this long
    for name in OWNERS:
        result[name] = {kind: totals.get((name, kind), [0, 0]) for kind in ("code", "data")}
    return result


TRACED_FILES = 40
TRACED_FEATHER_LINES = 80
TRACED_EXPLAINED_LINES, TRACED_STACKS = 5, 8
# pymalloc serves requests up to this many bytes; larger ones go to malloc.
SMALL_REQUEST_THRESHOLD = 512


def traced_memory(modules=None, owner=None):
    """Python's live allocations when Klipper runs under tracemalloc (PYTHONTRACEMALLOC=<frames> at its start), in
    bytes; None otherwise. Unlike getsizeof it sees every block Python's allocators hand out, also to C code (the
    engine's PyMem slot arrays), but not the engine's own malloc. "by_file" is the file of the innermost frame, so
    code loaded at import is the import machinery's; "by_owner" is the owner of the innermost frame that is not
    Python's, so work done in the stdlib or in an import counts for the Feather or Klipper code that asked for it.
    Klipper itself imports its extras, so a plugin's own top-level module code counts as Klipper's. Feather's share
    is split by its innermost line ("feather_lines": [kind, file:line, bytes]) and kind: "import" when the import
    machinery made the block below that line (the code and module of what the line imports), "run" otherwise. The
    largest of those lines keep their largest stacks ("feather_stacks": [kind, line, [[bytes, blocks, frames]]], the
    most recent frame first, ending at the line), which say what below the line holds the memory. Feather's "run"
    blocks are also summed per file ("feather_run_files": [file, phase, bytes]): phase "load" when an import frame is
    above the line (a module's own code while it loads: definitions, tables), "later" otherwise (also when the stack
    was cut before any import frame). "by_owner_large" is by_owner for blocks above SMALL_REQUEST_THRESHOLD, which
    Python takes from malloc: subtracted from malloc's in-use bytes it leaves the engine's native share."""
    import tracemalloc
    if not tracemalloc.is_tracing():
        return None
    modules = sys.modules if modules is None else modules
    owner = owner or owner_of
    names = dict((getattr(module, "__file__", None), name) for name, module in list(modules.items())
                 if module is not None and getattr(module, "__file__", None))
    owners = {}

    def file_owner(filename):
        if filename not in owners:
            owners[filename] = owner(names[filename]) if filename in names else PYTHON
        return owners[filename]

    def asking(frames):  # the innermost frame that is not Python's, or None
        return next((index for index, (name, _) in enumerate(frames) if file_owner(name) != PYTHON), None)

    def feather_key(frames, index):  # (kind, frame) of a Feather line; the frame is the trace's own (file, line)
        imported = any(name.startswith("<frozen importlib") for name, _ in frames[:index])
        return "import" if imported else "run", frames[index]

    by_file, by_owner, feather_lines = collections.Counter(), collections.Counter(), collections.Counter()
    by_owner_large, feather_run_files = collections.Counter(), collections.Counter()
    started = time.monotonic()
    traces = tracemalloc._get_traces()  # (domain, size, frames, ...), the most recent frame first
    # Two passes: the first counts, the second keeps the stacks of the largest lines only, so the report holds no
    # per-trace record (on the printer one per Feather trace was ~10 MB more at its peak).
    for trace in traces:
        size, frames = trace[1], trace[2]
        by_file[frames[0][0] if frames else "?"] += size
        index = asking(frames)
        asker = PYTHON if index is None else file_owner(frames[index][0])
        by_owner[asker] += size
        if size > SMALL_REQUEST_THRESHOLD:
            by_owner_large[asker] += size
        if asker in FEATHER_OWNERS:
            key = feather_key(frames, index)
            feather_lines[key] += size
            if key[0] == "run":
                loading = any(name.startswith("<frozen importlib") for name, _ in frames[index + 1:])
                feather_run_files[(frames[index][0], "load" if loading else "later")] += size
    explained = [key for key, _ in feather_lines.most_common(TRACED_EXPLAINED_LINES)]
    stacks = dict((key, collections.Counter()) for key in explained)
    blocks = collections.Counter()
    lines = set(frame for _, frame in explained)
    for trace in traces:
        size, frames = trace[1], trace[2]
        if not lines.intersection(frames):
            continue
        index = asking(frames)
        if index is None or file_owner(frames[index][0]) not in FEATHER_OWNERS:
            continue
        key = feather_key(frames, index)
        if key in stacks:
            below = frames[:index + 1]  # what is below the line, not who called it
            stacks[key][below] += size
            blocks[(key, below)] += 1
    count = len(traces)
    del traces
    current, peak = tracemalloc.get_traced_memory()
    return {"frames": tracemalloc.get_traceback_limit(), "traces": count, "current": current, "peak": peak,
            "overhead": tracemalloc.get_tracemalloc_memory(), "by_owner": dict(by_owner),
            "by_owner_large": dict(by_owner_large),
            "feather_run_files": [[name, phase, size] for (name, phase), size
                                  in feather_run_files.most_common(TRACED_FILES)],
            "by_file": dict(by_file.most_common(TRACED_FILES)),
            "feather_kinds": dict(sum((collections.Counter({kind: size}) for (kind, _), size in feather_lines.items()),
                                      collections.Counter())),
            "feather_lines": [[kind, "%s:%d" % line, size]
                              for (kind, line), size in feather_lines.most_common(TRACED_FEATHER_LINES)],
            "feather_stacks": [[kind, "%s:%d" % line,
                                [[size, blocks[((kind, line), frames)], ["%s:%d" % frame for frame in frames]]
                                 for frames, size in stacks[(kind, line)].most_common(TRACED_STACKS)]]
                               for kind, line in explained],
            "seconds": round(time.monotonic() - started, 3)}


# The packages and the entry point of Feather's own code.
FEATHER_MODULE_ROOTS = ("ui", "ff5m_ui", "feather", "feather_screen")


def feather_modules(loaded):
    """The Feather modules among loaded module names, sorted."""
    return sorted(name for name in loaded if name.split(".")[0] in FEATHER_MODULE_ROOTS)


def _engine():
    # The framework's own engine module: installed, or shipped inside the ui package on the printer, where it is
    # imported as ui.feather_engine.
    from ui.engine import feather_engine
    return feather_engine


def report(trim=True, read=None, engine=None, attribute=False, proc="/proc"):
    """The state of Klipper's memory now and, when trim is set, after malloc_trim(0): what the trim returns is
    memory the allocator holds free, not memory the process uses. The engine asks glibc, since the printer's Python
    has no ctypes. With attribute, the Python heap is also split by owner (attribution)."""
    engine = engine or _engine()
    python = python_state(engine)  # first: it collects unreachable cycles, whose nodes the census would count
    from . import MODULES_BEFORE
    result = {"time": time.time(), "pid": os.getpid(), "census": engine.census(), "python": python,
              "feather_modules": feather_modules(MODULES_BEFORE), "feather_modules_now": feather_modules(list(sys.modules)),
              "pymalloc": pymalloc_state(), "tracemalloc": traced_memory()}  # before the attribution allocates
    typer = typer_pid(proc)
    if typer is not None:
        try:
            result["typer"] = process_state(read, os.path.join(proc, str(typer), "status"))
        except OSError:
            pass  # Typer restarted between finding it and reading it
    if attribute:
        result["attribution"] = attribution(engine)
    malloc = engine.malloc_state()
    result["before"] = snapshot(malloc, read)
    if trim and malloc is not None:
        trimmed = engine.malloc_state(True)
        result["trimmed"] = trimmed["trimmed"]
        result["after_trim"] = snapshot(trimmed, read)
    return result


def save(result, directory=None):
    directory = directory or ARTIFACT_ROOT
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "memory-%d.json" % int(result["time"]))
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=1)
    return path
