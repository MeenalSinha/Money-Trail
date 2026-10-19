"""One declaration per argument produces both the JSON schema shown to the model and the validator.

Unknown arguments, wrong types (booleans are not integers), out-of-range numbers, values outside an enum and malformed
identifiers are all rejected before any handler (and therefore any query) runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Identifier shape accepted from the model: no leading/trailing whitespace, no control characters, quotes or backslashes.
# Identifiers are still always passed to the database as parameters; this keeps obvious junk out of the trace.
ID_PATTERN = re.compile(r"^\S(?:[^\x00-\x1f\x7f'\"\\]{0,198}\S)?$")


class ToolArgumentError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Arg:
    name: str
    type: str                                   # "string" | "integer"
    description: str
    required: bool = True
    enum: Optional[Tuple[str, ...]] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    is_id: bool = False


def json_schema(args: Sequence[Arg]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for a in args:
        p: Dict[str, Any] = {"type": a.type, "description": a.description}
        if a.enum:
            p["enum"] = list(a.enum)
        if a.minimum is not None:
            p["minimum"] = a.minimum
        if a.maximum is not None:
            p["maximum"] = a.maximum
        if a.is_id:
            p["maxLength"] = 200
        props[a.name] = p
    return {"type": "object", "properties": props, "required": [a.name for a in args if a.required],
            "additionalProperties": False}


def validate_args(args: Sequence[Arg], raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ToolArgumentError(["arguments must be a JSON object"])
    spec = {a.name: a for a in args}
    problems: List[str] = [f"unexpected argument '{k}'" for k in raw if k not in spec]
    out: Dict[str, Any] = {}
    for a in args:
        if a.name not in raw or raw[a.name] is None:
            if a.required:
                problems.append(f"missing required argument '{a.name}'")
            continue
        v = raw[a.name]
        if a.type == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                problems.append(f"'{a.name}' must be an integer")
                continue
            if a.minimum is not None and v < a.minimum:
                problems.append(f"'{a.name}' must be at least {a.minimum}")
                continue
            if a.maximum is not None and v > a.maximum:
                problems.append(f"'{a.name}' must be at most {a.maximum}")
                continue
        elif a.type == "string":
            if not isinstance(v, str):
                problems.append(f"'{a.name}' must be a string")
                continue
            if a.enum is not None and v not in a.enum:
                problems.append(f"'{a.name}' must be one of: {', '.join(a.enum)}")
                continue
            if a.is_id and not ID_PATTERN.match(v):
                problems.append(f"'{a.name}' is not a well-formed identifier")
                continue
        out[a.name] = v
    if problems:
        raise ToolArgumentError(problems)
    return out
