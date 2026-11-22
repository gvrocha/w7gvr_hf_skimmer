"""maidenhead.py -- Maidenhead grid locator from latitude/longitude.

Moved here from tools/watch_decodes.py so hsd can tag every spot with the
receiver's own grid square (rx_grid) using the same function.
"""


def grid_square(lat: float, lon: float) -> str:
    """6-character Maidenhead locator for (lat, lon), e.g. 'CN87ul'.

    Field (2 letters, 20x10 deg), square (2 digits, 2x1 deg), subsquare
    (2 lowercase letters, 5'x2.5'). Inputs are clamped just inside the
    valid range so the poles/antimeridian don't index off the end.
    """
    lat = min(max(lat, -90.0), 89.999999) + 90.0
    lon = min(max(lon, -180.0), 179.999999) + 180.0
    field_lon, field_lat = int(lon // 20), int(lat // 10)
    square_lon, square_lat = int((lon % 20) // 2), int(lat % 10)
    subsq_lon = int((lon % 2) * 12)
    subsq_lat = int((lat % 1) * 24)
    return "%c%c%d%d%c%c" % (
        ord("A") + field_lon, ord("A") + field_lat,
        square_lon, square_lat,
        ord("a") + subsq_lon, ord("a") + subsq_lat,
    )
