"""demo/run entry point (after the bootstrap installed what's missing).

./demo/run                 menu
./demo/run devops          prepare and start the DevOps demo (or `claims`, the claims-desk demo; `bank`, the bank teller demo)
./demo/run reset devops    back to the start of scene 1
./demo/run secure devops   scene 2 without Claude (--take claude-take: Claude's filmed answer)
./demo/run film devops     film the demo video (see demo/FILMING.md)
./demo/run status          what runs
./demo/run down            stop everything, Docker included
"""

from __future__ import annotations

import sys

from kit import demos, platform, ui


def menu() -> None:
    print()
    print(ui.bold("hexgate demo kit"))
    found = demos.all_demos()
    key = ui.choose("which demo?", [(d.key, d.title) for d in found])
    if key:
        demos.prepare(demos.get(key))


def main(argv: list[str]) -> int:
    try:
        if not argv:
            menu()
        elif argv[0] in ("-h", "--help", "help"):
            print(__doc__)
        elif argv[0] == "status":
            demos.status()
        elif argv[0] == "up":
            platform.up()
        elif argv[0] == "down":
            platform.down(docker="--keep-docker" not in argv)
        elif argv[0] == "film":
            if len(argv) < 2:
                print("usage: ./demo/run film <demo> [scene id ... | render] [--out DIR] [--raw VIDEO] [--no-join]")
                return 2
            from kit import film

            rest, opts = argv[2:], {}
            for flag in ("--out", "--raw"):
                if flag in rest[:-1]:
                    i = rest.index(flag)
                    opts[flag] = rest[i + 1]
                    rest = rest[:i] + rest[i + 2:]
            join = "--no-join" not in rest
            rest = [a for a in rest if a != "--no-join"]
            film.run(demos.get(argv[1]), rest, opts.get("--out"), opts.get("--raw"), join)
        elif argv[0] in ("reset", "secure", "app"):
            if len(argv) < 2:
                print(f"usage: ./demo/run {argv[0]} <demo>")
                return 2
            demo = demos.get(argv[1])
            if argv[0] == "reset":
                demos.reset(demo)
            elif argv[0] == "secure":
                take = argv[argv.index("--take") + 1] if "--take" in argv[:-1] else None
                demos.secure(demo, take)
            else:
                demos.start_app(demo, scripted="--scripted" in argv)
        else:
            demos.prepare(demos.get(argv[0]), open_browser="--no-browser" not in argv)
    except KeyboardInterrupt:
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
