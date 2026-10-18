"""Detector registry: the engine selects enabled detectors from here; adding one needs no engine change."""
from __future__ import annotations

from typing import Dict, Tuple, Type

from money_trail.investigation.detectors.base import Detector
from money_trail.investigation.detectors.circular import CircularDetector
from money_trail.investigation.detectors.fan_in import FanInDetector
from money_trail.investigation.detectors.fan_out import FanOutDetector
from money_trail.investigation.detectors.pass_through import PassThroughDetector

DETECTORS: Dict[str, Type[Detector]] = {
    CircularDetector.name: CircularDetector,
    PassThroughDetector.name: PassThroughDetector,
    FanInDetector.name: FanInDetector,
    FanOutDetector.name: FanOutDetector,
}
ORDER: Tuple[str, ...] = tuple(DETECTORS)


def get_detector_class(name: str) -> Type[Detector]:
    return DETECTORS[name]
