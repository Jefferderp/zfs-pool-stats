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


def shelllllllllllllllllllllllll(cmdline):
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
#iostat_vals = shell("zpool iostat -Hypl " + POOL_NAME + " " + REPEAT_DELAY + " " + "1").split()
iostat_vals = ["amalgm", "51567724367872", "16344298516480", "16", "0", "8468325", "0", "15682379", "-", "15682379", "-", "3532", "-", "3510", "-", "-", "-"]
iostat = dict(zip(iostat_keys, iostat_vals))  # Zip together `iostat` headers and values to create a dictionary

# print(iostat)
# for c, v in iostat.items():
#      print(c, v)

### Ingest data from `zfs get` ###
zfsget_keys = ["pCapVirtUsed", "pCapVirtFree", "pCapCompPerc", "pCapUsedBychilds", "pCapUsedBysnapshots"]
#zfsget_vals = shell("zfs get used,available,compressratio,usedbychildren " + POOL_NAME +
#                    " -Hp -d 0 -o value | tr '\n' ' '").split()
zfsget_vals = ["54866186481664", "12908397449216", "1.01", "54700434006016"]

# TODO: This `zfs get usedbysnapshots` command is very slow, because it's recursively checking
#       all snapshots sizes (-r) and summing them (awk) before returning. Both operations are intensive.
#       Also, this method with `grep` and `awk` is very clunky and may break with future `zfs` versions.

#zfsget_vals.extend(shell("zfs get usedbysnapshots " + POOL_NAME +
#                         " -Hp -r -o value | grep -v '-' | awk '{s+=$1} END {printf \"%.0f\", s}'").split())
zfsget_vals.extend(["1381425606656"])

zfsget = dict(zip(zfsget_keys, zfsget_vals))

# Ingest data from `zpool list`
zpoollist_keys = ["pStateHealth", "pStateFrag"]
#zpoollist_vals = shell("zpool list -H -o health,frag " + POOL_NAME)
zpoollist_vals = ["ONLINE", "20%"]

zpoollist = dict(zip(zfsget_keys, zfsget_vals))

# Ingest data from `zpool status`
zpoolstatus_keys = ["pStateDescript"]
# TODO: Clean up this bash mess and parse the values locally
#zpoolstatus_vals = shell("zpool status " + POOL_NAME +
#                         " | sed -n '3,$p' | tr '\n' ' ' | tr -d '\011\012' | sed -e 's/^[ \t]*//' | " +
#                         "sed --regexp-extended 's/ config\:.*//g'")
zpoolstatus_vals = ["scan: scrub repaired 0B in 1 days 12:59:37 with 0 errors on Sat Jan 27 22:59:39 2024 remove: Removal of mirror canceled on Tue Jan  9 08:30:58 2024"]

zpoolstatus = dict(zip(zfsget_keys, zfsget_vals))

# print("iostat", iostat)
# print("zfsget", zfsget)
# print("zpoollist", zpoollist)
# print("zpoolstatus", zpoolstatus)

for list in iostat, zfsget, zpoollist, zpoolstatus:
    for key, value in list.items():
        print(key, value)
