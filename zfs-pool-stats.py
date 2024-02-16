#! python3
import subprocess  # for shell()
import math  # for convert_sizes()

# TODO FEATURES:
# * Repeated output in aligned columns with automatic minimum width
# * Convert byte/nanosecond values to human-readable notation
# To conv_bytes() add:
# * Up/down/nearest rounding preference per-invocation
# * A sticky header with ["stateHealth"] and ["stateText"]
# * An -o flag to specify the list and order of columns (comma-separated)
# * An -o+ flag to add additional columns to the default set (a'la `lsblk`)
# * An -p flag to specify the pool name
# * An -t flag to specify the iteration frequency
# * Use first line of `zpool iostat` (without -y) to get statistics since boot, and display as the last line (sticky)
# * Implement color codes for pool state health
# * A brief warning if the user specifies a REPEAT_DELAY < 1
# * Add internal delays of between 5 and 15 seconds for any relatively stable values, which are highly unlikely
#       to change drastically in a brief time window. These are:
#       Name, LogicCapUsed, LogicCapFree, VirtCapUsed, VirtCapFree, VirtCompRatio,
#       VirtCapUsedByChilds, VirtCapUsedBySnaps, StateHealth, StateFrag, StateText
# * Implement multiple simultaneous pool outputs
# * Change shell_cmd() to run locally instead of remotely
# * Restore shell_cmd() lines commented out and remove placeholder lists

### Define constants ###
POOL_NAME = "amalgm"  # TODO: Get this as an external argument. Accept a string.
# Repeat delay also affects the sampling time of some commands like `zpool iostat`.
# A sampling time of at least 1 second is ideal for accurate statistics.
REPEAT_DELAY = "1.0"  # TODO: Get this as an external argument. Accept an int or float.


def shell_cmd(cmdline):
    """Run shell commands via SSH and return output.

    Args:
        cmdline: The shell command to run on the system.

    Returns:
        A string representing either stdout or stderr,
        depending on the exit code of cmdline.
    """
    result = subprocess.run(["ssh", "root@192.168.1.33", cmdline],
                            capture_output=True, text=True, check=True)

    # if result.returncode == 0:
    #     return result.stdout
    # else:
    #     return result.stderr
    return result.stdout if result.returncode == 0 else result.stderr


def conv_float(value):
    """Try to convert string to float. On fail, pass through unmodified.

    Args:
        value: The value (string or int) to convert to float.

    Returns:
        The float representation of value.
    """
    try:
        value = value.strip('-%')  # Remove undesired chars
        return float(value) if value else 0  # Convert eligible strings to floats. Convert empty strings to 0.
    except ValueError:  # If failed to convert to float, return as original type.
        return value


def print_struct(struct):
    """Pretty-print a struct (dict/list/tuple) in a line-delimited 'key : value' format."""
    for key, value in struct.items():
        print(f"{key} : {value}")


def conv_bytes(size, unit="", decimals=1):
    """Convert byte values to a specified unit. Uses powers of 1024 to align with `zfs get`.

    Args:
        size: The byte value to convert (int or float).
        notation: The desired notation ('B', 'K', 'M', 'G', 'T', 'P', 'E').
                  Defaults to 'M'.

    Returns:
        A string representing the byte value in the chosen format.
    """
    # Handle 0 and strings. Return unmodified.
    if size == 0 or isinstance(size, str):
        return size
    units = ("B", "K", "M", "G", "T", "P", "E")
    if unit == "":  # If unit not specified, calculate automatically
        i = int(math.floor(math.log(size, 1024)))  # How does math work?!
        p = math.pow(1024, i)
        output = round(size / p, decimals)
        return f"{output}{units[i]}"
    try:  # If unit specified, scale to that unit
        index = units.index(unit.upper())  # Find index of target notation
        divisor = 1024 ** index  # Calculate byte value to divide by
        output = round(size / divisor, decimals)
        return f"{output}{unit}"
    except ValueError:
        print(f"ValueError: Unit {unit} is not one of: {units}")


def conv_microseconds(time, unit=""):
    """TODO: Write this docstring.
    `zpool iostat` uses microseconds."""
    # Handle 0 and strings. Return unmodified.
    if time == 0 or isinstance(time, str):
        return time
    # Automatic unit scaling, if unspecified.
    if unit == "":
        factors = (1000000, 1000, 60, 60, 24)  # Scale factors for each unit
        units = "us ms s m h d".split()

        for i in range(len(factors)):
            scaled_time = time / factors[i]
            if scaled_time < 100:
                return f"{scaled_time} {units[i]}"
            time = scaled_time  # If >= 100, prepare to check the next unit

        return f"{time} d"  # Any final residue will be considered days
    # Manual unit scaling, if specified.
    units = {"d": 86400000000, "h": 3600000000, "m": 60000000, "s": 1000000, "ms": 1000, "us": 1}
    try:
        return f"{time / units[unit]} {unit}"  # TODO: THIS IS BROKEN
    except KeyError:
        print(f"ValueError: Unit {unit} is not one of: {units}")


def get_stats():
    """Ingest ZFS pool statistics from `iostat`, `zfs get` and `zpool status` system commands.

    Args:
        None

    Returns:
        A dictionary of ZFS pool statistics, formatted as strings and floats."""

    # NOTE: In case the output sequence from any of these underlying commands ever changes in a future version,
    #       the keys and values in this dictionary will be misaligned, requiring source code adjustment.

    #           Starting from ["Name"], the values of `zpool iostat` are assigned
    #           Starting from ["VirtCapUsed"], the values of `zfs get` are assigned
    #           Starting from ["StateHealth"], the values of `zfs get` (again) are assigned
    #           Starting from ["StateText"], the values of `zpool status` are assigned
    zpool_keys = ("Name", "LogicCapUsed", "LogicCapFree", "OpsRead", "OpsWrite", "BwRead", "BwWrite",
                  "TotalwaitRead", "TotalwaitWrite", "DiskwaitRead", "DiskwaitWrite", "SyncqwaitRead",
                  "SyncqwaitWrite", "AsyncqwaitRead", "AsyncqwaitWrite", "ScrubWait", "TrimWait",
                  "VirtCapUsed", "VirtCapFree", "VirtCompRatio", "VirtCapUsedByChilds", "VirtCapUsedBySnaps",
                  "StateHealth", "StateFrag", "StateText")

    zpool_vals = (["amalgm", "51567724367872", "16344298516480", "16", "0", "8468325", "0",
                   "15682379", "-", "15682379", "-", "3532", "-", "3510", "-", "-", "-"])
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
    zpool = {key: conv_float(value) for key, value in zpool.items()}

    # Create / update some more value pairs
    zpool.update({'VirtCapTot': zpool["VirtCapUsed"] + zpool["VirtCapFree"]})
    zpool.update({'VirtCapUsedPerc': round(zpool["VirtCapUsed"] / zpool["VirtCapTot"] * 100),
                  'VirtCompPerc': str(f"{zpool['VirtCompRatio'] -1 :.0%}"),
                  'TotalwaitBoth': zpool["TotalwaitRead"] + zpool["TotalwaitWrite"]})

    # Sort the dictionary alphabetically by key
    zpool = dict(sorted(zpool.items()))

    return zpool


zpool = get_stats()

print_struct(zpool)
