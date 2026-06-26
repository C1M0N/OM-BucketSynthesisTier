batch_4k.mjs and msdRunner.mjs must be placed INSIDE the calculator repo working dir
  "/tmp/calc/ManiaMapAnalyser by Leo_Black/"
so their relative imports (./js/..., ./msdRunner.mjs) resolve. run_4k.py copies/uses them
from that repo path. This folder is a persistence copy of the two engine-specific files only;
the repo itself (LeoBlackMT/osumania_map_analyser) is re-cloned if /tmp is wiped.
