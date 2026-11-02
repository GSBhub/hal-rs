"""PeakRDL plugin entry points (``peakrdl.exporters`` in pyproject.toml)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from peakrdl import process_input
from peakrdl.plugins.exporter import ExporterSubcommandPlugin
from systemrdl import RDLCompiler

from ..rdl import _HalrsIrq
from . import memory_source, model_source, write

if TYPE_CHECKING:
    import argparse

    from systemrdl.node import AddrmapNode


def _source(options: "argparse.Namespace") -> str:
    return ", ".join(Path(f).name for f in getattr(options, "input_files", []) or [])


class _Exporter(ExporterSubcommandPlugin):
    """PeakRDL registers plugin UDPs soft, i.e. only for files that declare
    them; hal-rs RDL may use ``halrs_irq`` undeclared, so register it hard then.
    Otherwise the same as ExporterSubcommandPlugin.main."""

    def main(self, importers, options: "argparse.Namespace") -> None:
        declared = any(re.search(r"\bproperty\s+halrs_irq\b", Path(f).read_text(errors="replace"))
                       for f in options.input_files if f.endswith(".rdl"))
        rdlc = RDLCompiler()
        rdlc.register_udp(_HalrsIrq, soft=declared)
        parameters = process_input.parse_parameters(rdlc, options.parameters)
        process_input.process_input(rdlc, importers, options.input_files, options)
        root = rdlc.elaborate(top_def_name=options.top_def_name, inst_name=options.inst_name,
                              parameters=parameters)
        self.do_export(root.top, options)


class ModelExporter(_Exporter):
    short_desc = "HALucinator/avatar2 peripheral models (Python) from an address map"

    def do_export(self, top_node: "AddrmapNode", options: "argparse.Namespace") -> None:
        write(options.output, model_source(top_node, _source(options)))


class MemoryExporter(_Exporter):
    short_desc = "HALucinator memory config (YAML peripherals: section) from an address map"

    def add_exporter_arguments(self, arg_group: "argparse._ActionsContainer") -> None:
        arg_group.add_argument("--module", help="Python module holding the models from "
                               "`peakrdl halucinator` (default: GenericPeripheral)")

    def do_export(self, top_node: "AddrmapNode", options: "argparse.Namespace") -> None:
        write(options.output, memory_source(top_node, options.module, _source(options)))
