"""Tier-2 function matching with Ghidra FunctionID (via pyghidra).

``build_fidb`` imports reference objects (see :mod:`halrs.refbuild`) into a
scratch Ghidra project and populates a ``.fidb`` with one library per variant.
``match`` loads stripped firmware, seeds functions from the Cortex-M vector
table, runs auto-analysis and FunctionID, and names a function only when every
match above the score threshold agrees on the name. The result is written in
HALucinator's addrs.yaml format, so it plugs straight into ``halrs run``.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

LANGUAGE = "ARM:LE:32:Cortex"
LIBRARY_FAMILY = "stm32f4xx_hal"
# Ghidra's FidAnalyzer default is 14.6; overall scores are code units matched.
DEFAULT_THRESHOLD = 14.6
# Minimum non-call instructions for a norm_hash match (FID-threshold-like).
NORM_MIN_UNITS = 10


def start() -> None:
    import pyghidra

    if pyghidra.started():
        return
    if (jh := paths.java_home()) and "JAVA_HOME" not in os.environ:
        os.environ["JAVA_HOME"] = str(jh)
    pyghidra.start(install_dir=paths.ghidra_install())


def _monitor():
    from ghidra.util.task import TaskMonitor

    return TaskMonitor.DUMMY


def _import(project, src: Path, folder: str, *, base: int | None = None):
    """Import `src` into `folder`; raw binaries are loaded at `base`."""
    import pyghidra
    from ghidra.app.util.opinion import BinaryLoader

    b = (pyghidra.program_loader().project(project).source(str(src))
         .projectFolderPath(folder).language(LANGUAGE).compiler("default"))
    if base is not None:
        b = b.loaders(BinaryLoader).addLoaderArg("-loader-baseAddr", hex(base))
    results = b.load()
    try:
        results.save(_monitor())
        return results.getPrimary().getSavedDomainFile()
    finally:
        results.close()


def _calls(program, by_addr: bool = False) -> dict[int, tuple[str, list]]:
    """Per function: (name, ordered callees). Callees are names when the target
    is named (reference objects: externals/locals) else entry addresses;
    `by_addr` always gives addresses (stripped targets)."""
    fm = program.getFunctionManager()
    listing = program.getListing()
    out = {}
    for f in fm.getFunctions(True):
        seq = []
        for ins in listing.getInstructions(f.getBody(), True):
            if not ins.getFlowType().isCall():
                continue
            for ref in ins.getReferencesFrom():
                if not ref.getReferenceType().isCall():
                    continue
                callee = fm.getFunctionAt(ref.getToAddress())
                if callee is None:
                    continue
                if callee.isThunk():
                    callee = callee.getThunkedFunction(True)
                addr = int(callee.getEntryPoint().getOffset())
                named = not str(callee.getName()).startswith("FUN_")
                seq.append(str(callee.getName()) if named and not by_addr else addr)
                break
        out[int(f.getEntryPoint().getOffset())] = (str(f.getName()), seq)
    return out


def _thumb2_branch(b: bytes) -> bool:
    """32-bit Thumb B<c>.W / B.W / BL / BLX: hw1 = 11110..., hw2 = 1x...."""
    return len(b) == 4 and (b[1] & 0xF8) == 0xF0 and (b[3] & 0x80) != 0


def norm_hash(program, func) -> tuple[int, str] | None:
    """FID-style full hash that is stable under call direction.

    Ghidra's ARM SLEIGH instruction mask for Thumb-2 branches keeps the offset's
    sign bit S (hw1 bit 10), so FID's full hash differs between a forward and a
    backward ``bl`` to the same callee. In reference objects every external call
    is forward (Ghidra's EXTERNAL block sits above the code) while linked
    firmware calls backwards about half the time, so FID can never match those
    functions. This mirrors MessageDigestFidHasher's full hash -- opcode bits
    under the instruction mask plus register operands, scalar/address values
    masked -- with S cleared. Returns (code units excluding calls, hex digest),
    or None for an empty body.
    """
    import hashlib

    from ghidra.program.model.address import Address
    from ghidra.program.model.lang import Register

    h = hashlib.blake2b(digest_size=8)
    units = 0
    for ins in program.getListing().getInstructions(func.getBody(), True):
        if str(ins.getMnemonicString()).lower() == "nop":
            continue
        proto = ins.getPrototype()
        raw = bytes(x & 0xFF for x in ins.getBytes())
        mask = bytes(x & 0xFF for x in proto.getInstructionMask().getBytes())
        masked = bytearray(x & y for x, y in zip(raw, mask))
        if _thumb2_branch(raw):
            masked[1] &= ~0x04
        ops = []
        for i in range(ins.getNumOperands()):
            if proto.getOperandValueMask(i) is None:
                continue
            for o in ins.getOpObjects(i):
                ops.append(str(o) if isinstance(o, Register) else "A" if isinstance(o, Address) else "S")
            ops.append("|")
        h.update(bytes(masked) + ",".join(ops).encode() + b";")
        if not ins.getFlowType().isCall():
            units += 1
    return (units, h.hexdigest()) if units else None


@dataclass
class LibraryStats:
    variant: str
    attempted: int
    added: int


def build_fidb(built: list, fidb: Path, workdir: Path) -> list[LibraryStats]:
    """Create `fidb` with one FID library per refbuild.Built variant."""
    start()
    import pyghidra
    from java.io import File
    from java.util import ArrayList
    from ghidra.feature.fid.db import FidFileManager
    from ghidra.feature.fid.service import FidService
    from ghidra.program.model.lang import LanguageID
    from ghidra.program.util import GhidraProgramUtilities

    workdir = workdir.resolve()  # Ghidra requires absolute project paths
    workdir.mkdir(parents=True, exist_ok=True)
    project = pyghidra.open_project(str(workdir), "halrs-fid-ref", create=True)
    stats = []
    try:
        variant_files = {}
        # name -> distinct ordered callee-name sequences across all variants.
        calls: dict[str, list[list[str]]] = {}
        # "units:hash" -> names, see norm_hash.
        norm: dict[str, set[str]] = {}
        for b in built:
            folder = "/" + b.variant.name
            files = []
            for obj in b.objects:
                df = project.getProjectData().getFile(f"{folder}/{obj.name}")
                if df is None:
                    df = _import(project, obj, folder)
                with pyghidra.program_context(project, df.getPathname()) as prog:
                    # Check the flag, not mere presence: an interrupted run can
                    # leave an imported-but-unanalyzed program behind.
                    if not GhidraProgramUtilities.isAnalyzed(prog):
                        pyghidra.analyze(prog)
                        prog.save("analyzed", _monitor())
                    for name, seq in _calls(prog).values():
                        seq = [c for c in seq if isinstance(c, str)]
                        if seq not in calls.setdefault(name, []):
                            calls[name].append(seq)
                    for f in prog.getFunctionManager().getFunctions(True):
                        if not f.isThunk() and (k := norm_hash(prog, f)):
                            norm.setdefault(f"{k[0]}:{k[1]}", set()).add(str(f.getName()))
                files.append(df)
            variant_files[b.variant] = files
        fidb.with_suffix(".calls.json").write_text(json.dumps(calls, sort_keys=True))
        fidb.with_suffix(".norm.json").write_text(
            json.dumps({k: sorted(v) for k, v in norm.items()}, sort_keys=True))

        if fidb.exists():
            fidb.unlink()
        fidb.parent.mkdir(parents=True, exist_ok=True)
        fm = FidFileManager.getInstance()
        fm.createNewFidDatabase(File(str(fidb)))
        fid_file = fm.addUserFidFile(File(str(fidb)))
        db = fid_file.getFidDB(True)
        try:
            svc = FidService()
            for v, files in variant_files.items():
                r = svc.createNewLibraryFromPrograms(
                    db, LIBRARY_FAMILY, v.hal, f"{v.cc}-{v.opt}", ArrayList(files), None,
                    LanguageID(LANGUAGE), None, None, _monitor())
                stats.append(LibraryStats(v.name, r.getTotalAttempted(), r.getTotalAdded()))
            db.saveDatabase("halrs reference libraries", _monitor())
        finally:
            db.close()
            fm.removeUserFile(fid_file)
    finally:
        project.close()
    return stats


@dataclass
class MatchResult:
    names: dict[int, str] = field(default_factory=dict)          # addr -> unique name
    tier: dict[int, str] = field(default_factory=dict)           # addr -> how it was named
    ambiguous: dict[int, list[str]] = field(default_factory=dict)  # addr -> candidates
    functions: int = 0


def _seed_vector_table(program, base: int, image: bytes, max_vectors: int = 256) -> None:
    """Create functions at every Cortex-M vector that points into the image."""
    from ghidra.program.flatapi import FlatProgramAPI

    api = FlatProgramAPI(program)
    seen = set()
    for i in range(1, min(max_vectors, len(image) // 4)):
        (v,) = struct.unpack_from("<I", image, 4 * i)
        addr = v & ~1
        if not (v & 1) or not (base <= addr < base + len(image)) or addr in seen:
            continue
        seen.add(addr)
        a = api.toAddr(addr)
        api.disassemble(a)
        if api.getFunctionAt(a) is None:
            api.createFunction(a, None)


def _norm_matches(program, ref_norm: dict[str, list[str]], skip: set[int]) -> dict[int, str]:
    """addr -> name where the function's norm_hash maps to one reference name
    and no other target function has the same hash."""
    by_key: dict[str, list[int]] = {}
    for f in program.getFunctionManager().getFunctions(True):
        addr = int(f.getEntryPoint().getOffset())
        if addr in skip or f.isThunk():
            continue
        k = norm_hash(program, f)
        if k and k[0] >= NORM_MIN_UNITS:
            by_key.setdefault(f"{k[0]}:{k[1]}", []).append(addr)
    return {addrs[0]: ref_norm[k][0] for k, addrs in by_key.items()
            if len(addrs) == 1 and len(ref_norm.get(k, ())) == 1}


def match(firmware: Path, fidb: Path, workdir: Path, *, base: int = 0x0800_0000,
          threshold: float = DEFAULT_THRESHOLD, irq_names: dict[int, str] | None = None,
          use_callgraph: bool = True) -> MatchResult:
    """Name functions in a raw firmware image loaded at `base`: vector table,
    then FunctionID, then call-graph propagation from those anchors."""
    from .callgraph import propagate, vector_names

    start()
    import pyghidra
    from java.io import File
    from ghidra.feature.fid.db import FidFileManager
    from ghidra.feature.fid.service import FidService

    workdir = workdir.resolve()  # Ghidra requires absolute project paths
    workdir.mkdir(parents=True, exist_ok=True)
    project = pyghidra.open_project(str(workdir), "halrs-match", create=True)
    fm = FidFileManager.getInstance()
    fid_file = fm.addUserFidFile(File(str(fidb)))
    try:
        name = firmware.name
        df = project.getProjectData().getFile("/" + name)
        if df is not None:
            df.delete()
        df = _import(project, firmware, "/", base=base)
        result = MatchResult()
        with pyghidra.program_context(project, df.getPathname()) as prog:
            with pyghidra.transaction(prog, "seed"):
                _seed_vector_table(prog, base, firmware.read_bytes())
            pyghidra.analyze(prog)
            result.functions = prog.getFunctionManager().getFunctionCount()

            # Match records read their names from the open FID database.
            query = fm.openFidQueryService(prog.getLanguage(), False)
            try:
                hits = FidService().processProgram(prog, query, threshold, _monitor())
                for h in hits or []:
                    addr = int(h.function.getEntryPoint().getOffset())
                    cands = sorted({str(m.getFunctionRecord().getName()) for m in h.matches
                                    if m.getOverallScore() >= threshold})
                    if len(cands) == 1:
                        result.names[addr] = cands[0]
                    elif cands:
                        result.ambiguous[addr] = cands
            finally:
                query.close()
            result.tier = {a: "fid" for a in result.names}

            norm_json = fidb.with_suffix(".norm.json")
            if norm_json.exists():
                ref_norm = json.loads(norm_json.read_text())
                for a, n in _norm_matches(prog, ref_norm, set(result.names)).items():
                    if n not in result.names.values():
                        result.names[a], result.tier[a] = n, "norm"
                        result.ambiguous.pop(a, None)

            image = firmware.read_bytes()
            for a, n in vector_names(image, base, irq_names).items():
                if a not in result.names and n not in result.names.values():
                    result.names[a], result.tier[a] = n, "vector"

            calls_json = fidb.with_suffix(".calls.json")
            if use_callgraph and calls_json.exists():
                ref_calls = json.loads(calls_json.read_text())
                target = {a: seq for a, (_, seq) in _calls(prog, by_addr=True).items()}
                for a, n in propagate(result.names, target, ref_calls).items():
                    result.names[a], result.tier[a] = n, "callgraph"
                    result.ambiguous.pop(a, None)
        return result
    finally:
        fm.removeUserFile(fid_file)
        project.close()


def write_addrs_yaml(names: dict[int, str], path: Path, *, base: int = 0,
                     entry: int = 0) -> Path:
    """HALucinator's addrs.yaml (as produced by hal_make_addr)."""
    import yaml

    doc = {"architecture": "ARMEL", "base_address": base, "entry_point": entry,
           "symbols": {a: n for a, n in sorted(names.items())}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path
