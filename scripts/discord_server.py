"""Start the official Haru MLX native server on a private Unix socket."""
import argparse
import os

from download_model import model_path


def server_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket")
    parser.add_argument("--language", choices=("ko", "ja"), default="ko")
    parser.add_argument("--quantize", choices=("8", "none"), default="8")
    args = parser.parse_args(argv)
    # 30ms is the upstream native default; avoids avoidable frame scheduling jitter.
    return ["--model", str(model_path()), "--quantize", args.quantize,
            "--language", args.language, "--socket", args.socket]


if __name__ == "__main__":
    # Restrict the native socket without changing the umask of importers.
    os.umask(0o077)
    from discord_native import main

    main(server_args())
