"""Fan-out detector: one account sending transfers to several distinct destination accounts in a window."""
from money_trail.investigation.detectors.fan_common import FanDetector, parse_fan_settings


class FanOutDetector(FanDetector):
    name = "fan_out"
    role = "source"
    focus_role_name = "distributor"
    counterparty_name = "recipients"
    parse_settings = staticmethod(parse_fan_settings)
