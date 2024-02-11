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


def shell(cmdline):
    """Run shell commands via SSH and return output (stdout or stderr).
    The only argument accepted is cmdline, the command to run."""
    result = subprocess.run(["ssh", "root@192.168.1.33", cmdline],
                            capture_output=True, text=True, check=False)

    if result.returncode == 0:
        return result.stdout
    else:
        return result.stderr


# TODO: Refactor all these into one master set of keys, which retreives values and appends as we go
### Ingest data from `iostat` ###
iostat_keys = ["pName", "pCapLogicUsed", "pCapLogicFree", "pOpsRead", "pOpsWrite", "pBwRead", "pBwWrite",
               "pTotalwaitRead", "pTotalwaitWrite", "pDiskwaitRead", "pDiskwaitWrite", "pSyncqwaitRead",
               "pSyncqwaitWrite", "pAsyncqwaitRead", "pAsyncqwaitWrite", "pScrubWait", "pTrimWait"]
iostat_vals = shell("zpool iostat -Hypl " + POOL_NAME + " " + REPEAT_DELAY + " " + "1").split()
iostat = dict(zip(iostat_keys, iostat_vals))  # Zip together `iostat` headers and values to create a dictionary

# print(iostat)
# for c, v in iostat.items():
#      print(c, v)

### Ingest data from `zfs get` ###
zfsget_keys = ["pCapVirtUsed", "pCapVirtFree", "pCapCompPerc", "pCapUsedBychilds", "pCapUsedBysnapshots"]
zfsget_vals = shell("zfs get used,available,compressratio,usedbychildren " + POOL_NAME +
                    " -Hp -d 0 -o value | tr '\n' ' '").split()

# TODO: This `zfs get usedbysnapshots` command is very slow, because it's recursively checking
#       all snapshots sizes (-r) and summing them (awk) before returning. Both operations are intensive.
#       Also, this method with `grep` and `awk` is very clunky and may break with future `zfs` versions.

zfsget_vals.extend(shell("zfs get usedbysnapshots " + POOL_NAME +
                         " -Hp -r -o value | grep -v '-' | awk '{s+=$1} END {printf \"%.0f\", s}'").split())

zfsget = dict(zip(zfsget_keys, zfsget_vals))

# Ingest data from `zpool list`
zpoollist_keys = ["pStateHealth", "pStateFrag"]
zpoollist_vals = shell("zpool list -H -o health,frag " + POOL_NAME)

zpoollist = dict(zip(zfsget_keys, zfsget_vals))

# Ingest data from `zpool status`
zpoolstatus_keys = ["pStateDescript"]
# TODO: Clean up this bash mess and parse the values locally
zpoolstatus_vals = shell("zpool status " + POOL_NAME +
                         " | sed -n '3,$p' | tr '\n' ' ' | tr -d '\011\012' | sed -e 's/^[ \t]*//' | " +
                         "sed --regexp-extended 's/ config\:.*//g'")

zpoolstatus = dict(zip(zfsget_keys, zfsget_vals))

print(zfsget, zpoollist, zpoolstatus)

for list in zfsget, zpoollist, zpoolstatus:
    for key, value in list:
        print(key, value)
