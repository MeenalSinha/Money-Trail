"""Fan-in detector: one account receiving transfers from several distinct source accounts in a window."""
from money_trail.investigation.detectors.fan_common import FanDetector, parse_fan_settings


class FanInDetector(FanDetector):
    name = "fan_in"
    role = "destination"
    focus_role_name = "collector"
    counterparty_name = "sources"
    parse_settings = staticmethod(parse_fan_settings)
