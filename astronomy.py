"""Cálculos geométricos para el gráfico; sin red ni ubicación de reserva.

Posición solar aproximada NOAA:
https://gml.noaa.gov/grad/solcalc/solareqns.PDF
Precisión suficiente para las franjas del gráfico (minutos, no efemérides).
"""
import calendar
import datetime as dt
import math

UTC = dt.timezone.utc


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def site_from_profile(profile):
    settings = profile.get("AstrometrySettings", {}) if isinstance(profile, dict) else {}
    lat, lon = number(settings.get("Latitude")), number(settings.get("Longitude"))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return {"latitude": lat, "longitude": lon, "elevation": number(settings.get("Elevation"))}


def solar_altitude(when, lat, lon):
    when = when.astimezone(UTC)
    hour = when.hour + when.minute / 60 + when.second / 3600
    gamma = 2 * math.pi / (366 if calendar.isleap(when.year) else 365) * (
        when.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eq = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                    - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    dec = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
           - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
           - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    ha = math.radians((hour * 60 + eq + 4 * lon) / 4 - 180)
    lat = math.radians(lat)
    return math.degrees(math.asin(max(-1, min(1,
        math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)))))


def night_at_connection(site, now=None):
    """Ventana fija de mediodía solar medio a mediodía siguiente, en UTC.

    No necesita adivinar la zona civil del observatorio. El navegador convierte
    las etiquetas a su hora local. Soporta noches/días polares sin eventos.
    """
    now = now or dt.datetime.now(UTC)
    offset = dt.timedelta(hours=site["longitude"] / 15)
    local_solar = now.astimezone(UTC) + offset
    start = local_solar.replace(hour=12, minute=0, second=0, microsecond=0)
    if local_solar < start:
        start -= dt.timedelta(days=1)
    start -= offset
    end = start + dt.timedelta(days=1)
    def altitude(t):
        return solar_altitude(t, site["latitude"], site["longitude"])
    def phase(a):
        return "day" if a >= -0.833 else "civil" if a >= -6 else "nautical" if a >= -12 else "astronomical" if a >= -18 else "night"
    events = []
    previous, prev_alt = start, altitude(start)
    for minute in range(5, 1441, 5):
        current = start + dt.timedelta(minutes=minute)
        current_alt = altitude(current)
        for threshold in (-0.833, -6, -12, -18):
            if (prev_alt < threshold) != (current_alt < threshold):
                low, high = previous, current
                for _ in range(10):
                    mid = low + (high - low) / 2
                    if (altitude(mid) < threshold) == (prev_alt < threshold):
                        low = mid
                    else:
                        high = mid
                events.append({"time": high.isoformat(), "altitude": threshold,
                               "direction": "rising" if current_alt > prev_alt else "setting",
                               "phase_after": phase(altitude(high))})
        previous, prev_alt = current, current_alt
    events.sort(key=lambda e: e["time"])
    return {"start": start.isoformat(), "end": end.isoformat(),
            "initial_phase": phase(altitude(start)), "events": events}
