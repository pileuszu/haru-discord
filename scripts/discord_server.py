"""Private Unix socket runtime for the Discord bridge; never opens a TCP port."""
import os
import sys
from download_model import model_path

os.umask(0o077)
from haru_mlx.native import main

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: discord_server.py SOCKET_PATH')
    main(['--model', str(model_path()), '--quantize', '8', '--language', 'ko',
          '--socket', sys.argv[1], '--warm-ms', '0'])
