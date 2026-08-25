from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BandRange:
    name: str
    lower_hz: int
    upper_hz: int


class BandResolver:
    """Map frequencies to canonical amateur-radio band names."""

    _BANDS = (
        BandRange("2200m", 135_700, 137_800),
        BandRange("630m", 472_000, 479_000),
        BandRange("160m", 1_800_000, 2_000_000),
        BandRange("80m", 3_500_000, 4_000_000),
        BandRange("60m", 5_000_000, 5_500_000),
        BandRange("40m", 7_000_000, 7_300_000),
        BandRange("30m", 10_100_000, 10_150_000),
        BandRange("20m", 14_000_000, 14_350_000),
        BandRange("17m", 18_068_000, 18_168_000),
        BandRange("15m", 21_000_000, 21_450_000),
        BandRange("12m", 24_890_000, 24_990_000),
        BandRange("10m", 28_000_000, 29_700_000),
        BandRange("6m", 50_000_000, 54_000_000),
        BandRange("4m", 70_000_000, 71_000_000),
        BandRange("2m", 144_000_000, 148_000_000),
        BandRange("1.25m", 222_000_000, 225_000_000),
        BandRange("70cm", 420_000_000, 450_000_000),
        BandRange("33cm", 902_000_000, 928_000_000),
        BandRange("23cm", 1_240_000_000, 1_300_000_000),
        BandRange("13cm", 2_300_000_000, 2_450_000_000),
        BandRange("9cm", 3_300_000_000, 3_500_000_000),
        BandRange("6cm", 5_650_000_000, 5_925_000_000),
        BandRange("3cm", 10_000_000_000, 10_500_000_000),
        BandRange("1.25cm", 24_000_000_000, 24_250_000_000),
        BandRange("6mm", 47_000_000_000, 47_200_000_000),
        BandRange("4mm", 76_000_000_000, 81_000_000_000),
        BandRange("2.5mm", 122_250_000_000, 123_000_000_000),
        BandRange("2mm", 134_000_000_000, 141_000_000_000),
        BandRange("1mm", 241_000_000_000, 250_000_000_000),
    )
    _NAMES = {band.name.lower(): band.name for band in _BANDS}

    def resolve(self, frequency_hz: int | float) -> str | None:
        frequency = int(frequency_hz)
        for band in self._BANDS:
            if band.lower_hz <= frequency <= band.upper_hz:
                return band.name
        return None

    def normalize(self, band: str) -> str | None:
        return self._NAMES.get(band.strip().lower())
