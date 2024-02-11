#! python3
import subprocess

# TODO FEATURES:
# * Repeated output in aligned columns with automatic minimum width
# * Convert byte/nanosecond values to human-readable notation
# * A sticky header with ["stateHealth"] and ["stateText"]
# * An -o flag to specify the list and order of columns (comma-separated)
# * An -o+ flag to add additional columns to the default set (a'la `lsblk`)
# * An -p flag to specify the pool name
# * An -t flag to specify the iteration frequency
# * Use first line of `zpool iostat` (without -y) to get statistics since boot, and display as the last line (sticky)
# * Implement multiple simultaneous pool outputs (`zpool iostat` can do this in a single run)
# * Implement color codes for pool state health

### Define constants ###
# TODO: Get this as an argument
POOL_NAME = "amalgm"
# Repeat delay also affects the sampling time of some commands like `zpool iostat`.
# A sampling time of at least 1 second is ideal for accurate statistics.
# TODO: Get this as an argument. The user should be able to enter "1", "1.0", "0.5", etc.
REPEAT_DELAY = "1.0"


# TODO: Change this function to run commands locally, once this script is complete.
def shell(cmdline):
    """Run shell commands via SSH and return output (stdout or stderr).
    (cmdline) is the command line to run."""
    result = subprocess.run(["ssh", "root@192.168.1.33", cmdline],
                            capture_output=True, text=True, check=True)

    # if result.returncode == 0:
    #     return result.stdout
    # else:
    #     return result.stderr
    return result.stdout if result.returncode == 0 else result.stderr


def str_to_float(value):
    """Attempt to convert (value) from string to float. If unable, return original string."""
    try:
        value = value.strip('-%')  # Remove undesired chars
        return float(value) if value else 0  # Convert eligible strings to floats. Convert empty strings to 0.
    except ValueError:  # If failed to convert to float, return as original type.
        return value


def print_struct(struct):
    """Print out a struct (dict, list or tuple), in both pretty and original format."""
    for key, value in struct.items():
        print(f"{key} : {value}")
    print("")  # Newline
    print(struct)


### Ingest data from `iostat`, `zfs get`, `zpool status` ###
# Starting from ["Name"], the values of `zpool iostat` are assigned
# Starting from ["VirtCapUsed"], the values of `zfs get` are assigned
# Starting from ["StateHealth"], the values of `zfs get` (again) are assigned
# Starting from ["StateText"], the values of `zpool status` are assigned
# NOTE: In case the output sequence from any of these underlying commands ever changes in a future version,
#       this dictionary will become misaligned, and require a re-evaluation of each underlying command.
zpool_keys = ("Name", "LogicCapUsed", "LogicCapFree", "OpsRead", "OpsWrite", "BwRead", "BwWrite",
              "TotalwaitRead", "TotalwaitWrite", "DiskwaitRead", "DiskwaitWrite", "SyncqwaitRead",
              "SyncqwaitWrite", "AsyncqwaitRead", "AsyncqwaitWrite", "ScrubWait", "TrimWait",
              "VirtCapUsed", "VirtCapFree", "VirtCompRatio", "VirtCapUsedByChilds", "VirtCapUsedBySnaps",
              "StateHealth", "StateFrag", "StateText")

zpool_vals = (["amalgm", "51567724367872", "16344298516480", "16", "0", "8468325",
               "0", "15682379", "-", "15682379", "-", "3532", "-", "3510", "-", "-", "-"])
# shell("zpool iostat -Hypl " + POOL_NAME + " " + REPEAT_DELAY + " " + "1").split()

zpool_vals.extend(["54866186481664", "12908397449216", "1.01", "54700434006016"])
# shell("zfs get used,available,compressratio,usedbychildren " + POOL_NAME + " -Hp -d 0 -o value | tr '\n' ' '").split()

# TODO: This `zfs get usedbysnapshots` command is very slow, because it's recursively checking
#       all snapshots sizes (`-r`) and summing them (`awk`) before returning. Alternative?
#       Also, this method with `grep` and `awk` is very clunky and may break with future `zfs` versions.
#       Parsing and addition should be performed locally.
zpool_vals.extend(["1381425606656"])
# shell("zfs get usedbysnapshots " + POOL_NAME + " -Hp -r -o value | grep -v '-' | awk '{s+=$1} END {printf \"%.0f\", s}'").split())

zpool_vals.extend(["ONLINE", "20%"])
# shell("zpool list -H -o health,frag " + POOL_NAME)

# TODO: Clean up this `zpool status` command and perform text parsing locally instead.
zpool_vals.extend(
    ["scan: scrub repaired 0B in 1 days 12:59:37 with 0 errors on Sat Jan 27 22:59:39 2024 remove: Removal of mirror canceled on Tue Jan  9 08:30:58 2024"])
# shell("zpool status " + POOL_NAME + " | sed -n '3,$p' | tr '\n' ' ' | tr -d '\011\012' | sed -e 's/^[ \t]*//' | " + "sed --regexp-extended 's/ config\:.*//g'")

# Merge key and values lists into a dictionary
zpool = dict(zip(zpool_keys, zpool_vals))

# Convert all eligible strings to floats
zpool = {key: str_to_float(value) for key, value in zpool.items()}

# Create / update some more value pairs
zpool.update({'VirtCapTot': zpool["VirtCapUsed"] + zpool["VirtCapFree"]})
zpool.update({'VirtCapUsedPerc': round(zpool["VirtCapUsed"] / zpool["VirtCapTot"] * 100),
              'VirtCompPerc': str(f"{zpool['VirtCompRatio'] -1:.0%}"),
              'TotalwaitBoth': zpool["TotalwaitRead"] + zpool["TotalwaitWrite"]})

# Sort the dictionary alphabetically by key
zpool = dict(sorted(zpool.items()))

# Print
print_struct(zpool)
