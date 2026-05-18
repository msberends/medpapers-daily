import urllib.parse

THEME_PRIMARY_COLOURS: dict[str, str] = {
    "default":   "#0d6efd",
    "cerulean":  "#2fa4e7",
    "cosmo":     "#2780e3",
    "flatly":    "#2c3e50",
    "journal":   "#eb6864",
    "litera":    "#4582ec",
    "lumen":     "#158cba",
    "lux":       "#0d6efd",
    "materia":   "#2196f3",
    "minty":     "#78c2ad",
    "morph":     "#378dfc",
    "pulse":     "#593196",
    "quartz":    "#0d6efd",
    "sandstone": "#325d88",
    "simplex":   "#d9230f",
    "sketchy":   "#333333",
    "spacelab":  "#446e9b",
    "united":    "#e95420",
    "yeti":      "#008cba",
    "zephyr":    "#3459e6",
    "cyborg":    "#2a9fd6",
    "darkly":    "#375a7f",
    "slate":     "#0d6efd",
    "solar":     "#268bd2",
    "superhero": "#df691a",
    "vapor":     "#6f42c1",
}

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="{colour}">'
    '<path fill-rule="evenodd" d="M6 1h6v7a.5.5 0 0 1-.757.429L9 7.083 6.757 8.43A.5.5 0 0 1 6 8z"/>'
    '<path d="M3 0h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2v-1h1v1a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V2a1 1 0 0 0-1-1H3a1 1 0 0 0-1 1v1H1V2a2 2 0 0 1 2-2"/>'
    '<path d="M1 5v-.5a.5.5 0 0 1 1 0V5h.5a.5.5 0 0 1 0 1h-2a.5.5 0 0 1 0-1zm0 3v-.5a.5.5 0 0 1 1 0V8h.5a.5.5 0 0 1 0 1h-2a.5.5 0 0 1 0-1zm0 3v-.5a.5.5 0 0 1 1 0v.5h.5a.5.5 0 0 1 0 1h-2a.5.5 0 0 1 0-1z"/>'
    '</svg>'
)


def favicon_href(theme_name: str) -> str:
    colour = THEME_PRIMARY_COLOURS.get(theme_name, THEME_PRIMARY_COLOURS["default"])
    svg = _FAVICON_SVG.format(colour=colour)
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="")


THEME_URLS: dict[str, str] = {
    "default":   "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
    # Bootswatch light themes (alphabetical)
    "cerulean":  "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cerulean/bootstrap.min.css",
    "cosmo":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cosmo/bootstrap.min.css",
    "flatly":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/flatly/bootstrap.min.css",
    "journal":   "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/journal/bootstrap.min.css",
    "litera":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/litera/bootstrap.min.css",
    "lumen":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/lumen/bootstrap.min.css",
    "lux":       "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/lux/bootstrap.min.css",
    "materia":   "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/materia/bootstrap.min.css",
    "minty":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/minty/bootstrap.min.css",
    "morph":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/morph/bootstrap.min.css",
    "pulse":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/pulse/bootstrap.min.css",
    "quartz":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/quartz/bootstrap.min.css",
    "sandstone": "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/sandstone/bootstrap.min.css",
    "simplex":   "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/simplex/bootstrap.min.css",
    "sketchy":   "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/sketchy/bootstrap.min.css",
    "spacelab":  "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/spacelab/bootstrap.min.css",
    "united":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/united/bootstrap.min.css",
    "yeti":      "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/yeti/bootstrap.min.css",
    "zephyr":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/zephyr/bootstrap.min.css",
    # Bootswatch dark themes
    "cyborg":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cyborg/bootstrap.min.css",
    "darkly":    "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css",
    "slate":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/slate/bootstrap.min.css",
    "solar":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/solar/bootstrap.min.css",
    "superhero": "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/superhero/bootstrap.min.css",
    "vapor":     "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/vapor/bootstrap.min.css",
}

FALLBACK_URL = THEME_URLS["default"]
VALID_THEMES = list(THEME_URLS.keys())


def get_theme_url(name: str) -> str:
    return THEME_URLS.get(name, FALLBACK_URL)
