"""Build a portable app-only source ZIP, excluding caches, credentials and Colibri."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
APP_FILES = (
    "server.py", "feeds.py", "hazards.py", "emergency.py", "map_data.py",
    "ml.py", "fire_ml.py", "copernicus.py", "local_geo.py", "local_helpers.py",
    "local_ml.py", "Dockerfile", ".dockerignore", "render.yaml", "README.md",
    "package_release.py", "network.py", "incidents.py", "build_network.py", "data/burgas_south.json",
)
STATIC_FILES = (
    "index.html", "overview.html", "impact.html", "impact.js", "operations.html", "operations.js", "app.js", "rain_renderer.js",
    "style.css", "manifest.webmanifest", "offline.html", "sw.js",
    "app-icon.svg", "app-icon-192.png", "app-icon-512.png",
)
TEST_FILES = (
    "test_copernicus.py", "test_destinations.py", "test_emergency.py",
    "test_feeds.py", "test_map_data.py", "test_ml.py", "test_release.py", "test_network.py", "test_incidents.py",
    "test_product_ui.js", "test_location.js", "test_requests.js",
    "test_water.js", "test_mobile.js", "test_rain_view.js",
    "test_rain_renderer.js", "test_phone_app.js",
)


def build_release(output):
    files = [ROOT / name for name in APP_FILES]
    files += [ROOT / "static" / name for name in STATIC_FILES]
    files += [ROOT / name for name in TEST_FILES]
    for file in files:
        if not file.is_file() or file.is_symlink():
            raise ValueError(f"Release input missing or not a regular file: {file.name}")
    if output.exists():
        raise ValueError("Output already exists; choose a new ZIP path.")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.relative_to(ROOT).as_posix())
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = build_release(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not package app: {error}\n")
    print(f"App-only deployment ZIP: {output.resolve()}")


if __name__ == "__main__":
    main()
