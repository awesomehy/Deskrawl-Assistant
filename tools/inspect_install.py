"""Read-only Deskrawl installation probe. Does not read credentials or player values."""

import argparse
import json
import re
from pathlib import Path


def probe(root: Path) -> dict:
    root = root.resolve()
    save = root / "Data" / "save.json"
    metadata = root / "Deskrawl_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    result = {
        "installation_found": (root / "Deskrawl.exe").is_file(),
        "engine": "Unity IL2CPP" if metadata.is_file() and (root / "GameAssembly.dll").is_file() else "unconfirmed",
        "steam_appid": None,
        "steam_buildid": None,
        "save": {"exists": save.is_file(), "decoded": False},
        "capabilities": {
            "static_localization_export": "verified separately by extract_catalog.py",
            "player_equipment_import": "not implemented; save is not decoded",
            "game_window_capture": "not verified; helper returned access denied during investigation",
        },
    }
    manifest = root.parent.parent / "appmanifest_4623570.acf"
    if manifest.is_file():
        source = manifest.read_text(encoding="utf-8", errors="replace")
        for field in ("appid", "buildid"):
            match = re.search(r'"' + field + r'"\s+"([0-9]+)"', source)
            if match:
                result["steam_" + field] = match.group(1)
    if save.is_file():
        try:
            # Only read the four-byte magic, never the saved user payload.
            with save.open("rb") as stream:
                header = stream.read(4)
            result["save"].update({
                "bytes": save.stat().st_size,
                "header": header.decode("ascii", errors="replace"),
                "plain_json": False if header in (b"DDS1", b"DDS2") else "unconfirmed",
            })
        except OSError as exc:
            result["save"]["read_error"] = type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=Path("G:/SteamLibrary/steamapps/common/Deskrawl"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out:
        target = args.out.resolve()
        if target == args.game_dir.resolve() or args.game_dir.resolve() in target.parents:
            parser.error("Output must be outside the game installation directory.")
    result = probe(args.game_dir)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["installation_found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
