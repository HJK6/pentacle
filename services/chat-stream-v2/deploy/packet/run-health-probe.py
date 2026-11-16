"""Run the packet probe and always preserve per-attempt outcome/transport facts."""
import importlib.util,json,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('packet_probe',Path(__file__).with_name('health-probe.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
try:print(json.dumps(m.probe(sys.argv[1],sys.argv[2])))
finally:Path(sys.argv[3]).write_text(json.dumps({'attempts':m.ATTEMPTS},indent=2)+'\n')
