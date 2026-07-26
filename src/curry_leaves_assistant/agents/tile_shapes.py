"""Structured output models for dashboard tiles.

One pydantic model per structured tile format. Passed as the kernel's
``Agent.output_type`` so a tile run returns validated JSON instead of free text
that dashboard_store.shape_output has to scrape with regexes. Each model carries
an explicit ``empty`` flag so the agent *declares* "nothing to report" rather
than us guessing from phrasing.

``to_wire()`` maps a validated instance back to the exact ``TileShapedOutput``
wire shape ({format, value, empty}) the board files and the renderer already
use, so nothing downstream changes shape.

The "markdown" format is intentionally absent from OUTPUT_TYPES — it keeps the
free-text path (markdownTemplate skeletons, long-form docs) untouched.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SummaryOutput(BaseModel):
    """A short prose summary."""

    text: str = Field(description="2-5 sentence summary; inline markdown (bold, links) allowed.")
    empty: bool = Field(description="True when there is nothing to report; text may then be ''.")

    def to_wire(self) -> dict:
        return {"format": "summary", "value": self.text, "empty": self.empty}


class ListOutput(BaseModel):
    """A short bulleted list."""

    items: list[str] = Field(description="One short line per item; inline markdown allowed.")
    empty: bool = Field(description="True when there is nothing to report; items may then be [].")

    def to_wire(self) -> dict:
        return {"format": "list", "value": self.items, "empty": self.empty or not self.items}


class MetricOutput(BaseModel):
    """A single headline number."""

    value: str = Field(description="The headline number, e.g. '14' or '97.3%'.")
    label: Optional[str] = Field(default=None, description="Short caption under the number, e.g. 'unread emails'.")
    delta: Optional[str] = Field(default=None, description="Optional trend vs the previous period, e.g. '+3 since yesterday'. Omit when unknown.")
    empty: bool = Field(description="True when there is nothing to report (no meaningful number this run).")

    def to_wire(self) -> dict:
        return {"format": "metric",
                "value": {"value": self.value, "label": self.label, "delta": self.delta},
                "empty": self.empty}


class TableOutput(BaseModel):
    """A small table."""

    header: list[str] = Field(description="Column headings.")
    rows: list[list[str]] = Field(description="Data rows, each with one cell per heading.")
    empty: bool = Field(description="True when there is nothing to report; rows may then be [].")

    def to_wire(self) -> dict:
        return {"format": "table", "value": {"header": self.header, "rows": self.rows},
                "empty": self.empty or not self.rows}


class DiffLine(BaseModel):
    kind: Literal["add", "remove", "context"] = Field(
        description="'add' for a new item, 'remove' for a gone item, 'context' for an unchanged item worth showing.")
    text: str = Field(description="The item, one line, no +/- prefix.")


class DiffOutput(BaseModel):
    """A change list rendered diff-style."""

    lines: list[DiffLine] = Field(description="One entry per changed (or context) item.")
    empty: bool = Field(description="True when nothing changed; lines may then be [].")

    def to_wire(self) -> dict:
        return {"format": "diff",
                "value": [{"kind": l.kind, "text": l.text} for l in self.lines],
                "empty": self.empty or not self.lines}


# Tile outputFormat -> output_type for the run. "markdown" is deliberately absent
# (free-text path); anything unlisted also falls back to free text.
OUTPUT_TYPES: dict[str, type[BaseModel]] = {
    "summary": SummaryOutput,
    "list": ListOutput,
    "metric": MetricOutput,
    "table": TableOutput,
    "diff": DiffOutput,
}

# dashboard_store keeps its own STRUCTURED_FORMATS literal (to stay free of
# agents-layer imports); this import direction (agents -> stores) is the allowed one.
from curry_leaves_assistant.stores.dashboard_store import STRUCTURED_FORMATS as _STRUCTURED_FORMATS  # noqa: E402

assert set(OUTPUT_TYPES) == set(_STRUCTURED_FORMATS), \
    "tile_shapes.OUTPUT_TYPES and dashboard_store.STRUCTURED_FORMATS drifted apart"
