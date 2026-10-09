from datetime import UTC, datetime

from app.contracts import Availability, Quality
from app.normalization.normalizer import Normalizer
from app.providers.base import Reading

NOW = datetime(2026, 1, 1, tzinfo=UTC)
N = Normalizer()


def r(value, unit="percent", **kw):  # type: ignore[no-untyped-def]
    return Reading("cpu.usage_percent", "cpu", value, unit, "test", **kw)


def test_decikelvin_converted_to_celsius() -> None:
    s = N.normalize(r(3542, "decikelvin"), NOW)
    assert s.unit == "celsius"
    assert s.value == 81.05
    assert s.quality is Quality.GOOD


def test_milliwatts_and_milliwatt_hours() -> None:
    assert N.normalize(r(12973, "mV"), NOW).value == 12.973
    assert N.normalize(r(44450, "mWh"), NOW).value == 44.45
    assert N.normalize(r(15000, "mW"), NOW).unit == "W"


def test_none_is_unavailable_with_reason_never_a_guess() -> None:
    s = N.normalize(Reading.unavailable("cpu.temperature_c", "cpu", "celsius", "LHM", "not exposed"), NOW)
    assert s.value is None
    assert s.quality is Quality.UNAVAILABLE
    assert s.availability is Availability.UNAVAILABLE
    assert s.reason == "not exposed"


def test_failed_reading_is_error() -> None:
    s = N.normalize(Reading.failed("x.y", "cpu", "percent", "src", "boom"), NOW)
    assert s.quality is Quality.ERROR


def test_nan_and_out_of_bounds_rejected_not_clamped() -> None:
    nan = N.normalize(r(float("nan")), NOW)
    assert nan.quality is Quality.ERROR and nan.value is None
    hot = N.normalize(r(9000, "decikelvin"), NOW)  # 626 °C: impossible
    assert hot.quality is Quality.ERROR and hot.value is None
    assert "Implausible" in (hot.reason or "")


def test_turbo_performance_above_100_allowed() -> None:
    s = N.normalize(r(258.0, "percent_of_nominal"), NOW)
    assert s.quality is Quality.GOOD and s.value == 258.0


def test_strings_and_bools_pass_through() -> None:
    assert N.normalize(r("charging", "state"), NOW).value == "charging"
    assert N.normalize(r(True, "bool"), NOW).value is True
