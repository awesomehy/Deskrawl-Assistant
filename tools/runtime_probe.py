"""Read Deskrawl containers externally; no injection or game writes."""
from pathlib import Path
import argparse
import json
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from deskrawl_assistant.runtime_client import RuntimeClient, write_runtime_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["diagnostics", "class", "registry", "snapshot"])
    parser.add_argument("class_name", nargs="?")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--image", default="Assembly-CSharp")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--max-items", type=int, default=2048)
    args = parser.parse_args()
    with RuntimeClient() as client:
        diagnostics = client.connect(args.pid)
        if args.operation == "diagnostics":
            result = diagnostics
        elif args.operation == "class":
            if not args.class_name:
                parser.error("class requires class_name")
            result = client.call("describeclass", args.class_name, args.namespace, args.image)
        elif args.operation == "registry":
            result = client.call("discoverregistry")
        else:
            result = client.snapshot(max_items=args.max_items)
        filename = f"{args.operation}-{args.class_name or 'latest'}.json"
        target = write_runtime_json(filename, result)
        print(json.dumps({"output": str(target), "complete": result.get("complete"),
            "issues": result.get("issues"), "counts": {k: {f: v for f, v in value.items() if f != "slots"}
                for k, value in result.get("containers", {}).items()}, "backend": result.get("backend")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
