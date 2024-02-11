#! python3
import subprocess

# TODO FEATURES:
# * A -o flag to specify the list and order of columns (comma-separated)
# * A -o+ flag to add additional columns to the default set (a'la `lsblk`)
# * A -p flag to specify the pool name
# * A -t flag to specify the iteration frequency
# * Use first line of `zpool iostat` (without -y) to get statistics since boot, and display as the last line (sticky)
# * Implement multiple simultaneous pool outputs (`zpool iostat` can do this in a single run)

### Define constants ###
# TODO: Get this as an argument
POOL_NAME = "amalgm"
# Repeat delay also affects the sampling time of some commands like `zpool iostat`.
# A sampling time of at least 1 second is ideal for accurate statistics.
# TODO: Get this as an argument. The user should be able to enter "1", "1.0", "0.5", etc.
REPEAT_DELAY = "1.0"


# TODO: Rewrite this function to run commands locally, once this script is complete.
def shell(cmdline):
    """Run shell commands via SSH and return output (stdout or stderr).
    The only argument accepted is cmdline, the command to run."""
    result = subprocess.run(["ssh", "root@192.168.1.33", cmdline],
                            capture_output=True, text=True, check=True)

    if result.returncode == 0:
        return result.stdout
    else:
        return result.stderr


### Ingest data from `iostat`, `zfs get`, `zpool status` ###

# Starting from ["pName"], the values of `zpool iostat` will be assigned
# Starting from ["pCapVirtUsed"], the values of `zfs get` will be assigned
# Starting from ["pStateHealth"], the values of `zfs get` (again) will be assigned
# Starting from ["pStateText"], the values of `zpool status` will be assigned

# In case the output sequence from any of these underlying commands ever changes in a future version,
# this dictionary will become misaligned, and require a re-evaluation of each underlying command.

stats_keys = ("pName", "pCapLogicUsed", "pCapLogicFree", "pOpsRead", "pOpsWrite", "pBwRead", "pBwWrite",
              "pTotalwaitRead", "pTotalwaitWrite", "pDiskwaitRead", "pDiskwaitWrite", "pSyncqwaitRead",
              "pSyncqwaitWrite", "pAsyncqwaitRead", "pAsyncqwaitWrite", "pScrubWait", "pTrimWait",
              "pCapVirtUsed", "pCapVirtFree", "pCapCompPerc", "pCapUsedBychilds", "pCapUsedBysnapshots",
              "pStateHealth", "pStateFrag", "pStateText")

stats_vals = []

stats_vals.extend(["amalgm", "51567724367872", "16344298516480", "16", "0", "8468325",
                   "0", "15682379", "-", "15682379", "-", "3532", "-", "3510", "-", "-", "-"])
# shell("zpool iostat -Hypl " + POOL_NAME + " " + REPEAT_DELAY + " " + "1").split()

stats_vals.extend(["54866186481664", "12908397449216", "1.01", "54700434006016"])
# shell("zfs get used,available,compressratio,usedbychildren " + POOL_NAME + " -Hp -d 0 -o value | tr '\n' ' '").split()

# TODO: This `zfs get usedbysnapshots` command is very slow, because it's recursively checking
#       all snapshots sizes (`-r`) and summing them (`awk`) before returning.
#       Also, this method with `grep` and `awk` is very clunky and may break with future `zfs` versions.
#       Parsing and addition should be performed locally.
stats_vals.extend(["1381425606656"])
# shell("zfs get usedbysnapshots " + POOL_NAME + " -Hp -r -o value | grep -v '-' | awk '{s+=$1} END {printf \"%.0f\", s}'").split())

stats_vals.extend(["ONLINE", "20%"])
# shell("zpool list -H -o health,frag " + POOL_NAME)

# TODO: Clean up this `zpool status` command and perform text parsing locally instead.
stats_vals.extend(["scan: scrub repaired 0B in 1 days 12:59:37 with 0 errors on Sat Jan 27 22:59:39 2024 remove: Removal of mirror canceled on Tue Jan  9 08:30:58 2024"])
# shell("zpool status " + POOL_NAME + " | sed -n '3,$p' | tr '\n' ' ' | tr -d '\011\012' | sed -e 's/^[ \t]*//' | " + "sed --regexp-extended 's/ config\:.*//g'")

# Merge key and values lists into a dictionary
stats = dict(zip(stats_keys, stats_vals))

# Print them out for visual inspection
for key, value in stats.items():
    print(f"{key} : {value}")
