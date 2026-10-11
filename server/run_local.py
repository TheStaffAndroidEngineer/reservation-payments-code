"""Start the two independent loopback processes; retain both ledgers across runs."""
import argparse
import subprocess
import sys
import time
from pathlib import Path

if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',default='runtime')
    args=parser.parse_args();directory=Path(args.directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    here=Path(__file__).resolve().parent
    commands=[['provider.py','--database',str(directory/'provider.sqlite')],
        ['booking.py','--database',str(directory/'booking.sqlite')]]
    processes=[]
    try:
        for command in commands:
            processes.append(subprocess.Popen([sys.executable,str(here/command[0]),*command[1:]]))
        print('Seatlight keeps payment and booking in separate processes/databases. Ctrl-C stops both without deleting data.',flush=True)
        while all(process.poll() is None for process in processes): time.sleep(.25)
        raise SystemExit('A service stopped; check for occupied ports8900/8901.')
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes: process.wait(timeout=5)
