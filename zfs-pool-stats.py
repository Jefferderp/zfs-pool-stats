#! python3
import subprocess
import math

""" TODO FEATURES:
* Repeated output in aligned columns with automatic minimum width
* Upgrade conv_microseconds() to round up by length (99s > 1.65m), instead of time (59s > 1m)
Add to conv_bytes():
* Up/down/nearest rounding preference per-invocation or per-value
* A sticky header with ["stateHealth"] and ["stateText"]
* An -o flag to specify the list and order of columns (comma-separated)
* An -o+ flag to add additional columns to the default set (a'la `lsblk`)
* An -p flag to specify the pool name
* An -t flag to specify the iteration frequency
* Use first line of `zpool iostat` (without -y) to get statistics since boot, and display as the last line (sticky)
   and with some special stylization (bold, underlined, etc.)
* Implement color codes for pool state health
* A brief warning if the user specifies a REPEAT_DELAY < 1
* Add internal delays of between 5 and 15 seconds for any relatively stable values, which are highly unlikely
      to change drastically in a brief time window. These are:
      Name, LogicCapUsed, LogicCapFree, VirtCapUsed, VirtCapFree, VirtCompRatio,
      VirtCapUsedByChilds, VirtCapUsedBySnaps, StateHealth, StateFrag, StateText
* Implement multiple simultaneous pool outputs
* Restore shell_cmd() lines commented out and remove placeholder lists
* Change shell_cmd() to run locally instead of remotely
* Try removing the need for math module
"""

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
        # Special handling for tuples: only print first value from any tuple
        if isinstance(key, tuple) and isinstance(value, tuple):  # If key and value are both tuples
            print(f"{key[0]} : {value[0]}")
        elif isinstance(key, tuple):  # If only key is a tuple
            print(f"{key[0]} : {value}")
        elif isinstance(value, tuple):  # If only value is a tuple
            print(f"{key} : {value[0]}")
        # If neither key or value are tuples, print them in their entirety:
        else:
            print(f"{key} : {value}")


def conv_bytes(size, notation=""):
    """Convert byte values to a specified notation. Uses powers of 1024 as output by `zfs get`.

    Args:
        size: The byte value (int or float) to convert.
        notation: The desired notation ('B', 'K', 'M', 'G', 'T', 'P', 'E').
            If unspecified, automatically chooses the highest notation.

    Returns:
        A string representing the byte value expressed in the chosen notation."""
    # Handle 0 and strings. Return them unmodified.
    if size == 0 or isinstance(size, str):
        return size

    notations = ("B", "K", "M", "G", "T", "P", "E")

    if notation == "":  # Automatic unit scaling, if not specified.
        i = int(math.floor(math.log(size, 1024)))  # Math, how does it work?!
        p = math.pow(1024, i)
        return f"{round(size / p)}{notations[i]}"

    try:  # Manual unit scaling, if specified.
        index = notations.index(notation.upper())  # Find index of target notation
        divisor = 1024 ** index  # Calculate byte value to divide by
        return f"{round(size / divisor)}{notation}"
    except ValueError:
        print(f"ValueError: {notation} is not one of: {notations}")


def conv_microseconds(time, notation="",):
    """Convert microsecond values to a specified notation. Accepts microseconds as output by `zpool iostat -p`.

    Args:
        time: The microsecond value (int or float) to convert.
        notation: The desired notation ('d', 'h', 'm', 's', 'ms', 'us').
            If unspecified, automatically chooses the highest notation.

    Returns:
        A string representing the time value expressed in the chosen notation."""
    # Handle 0 and strings. Return them unmodified.
    if time == 0 or isinstance(time, str):
        return time

    notations = {"d": 86400000000, "h": 3600000000, "m": 60000000, "s": 1000000, "ms": 1000, "us": 1}

    if notation == "":  # Automatic unit scaling, if not specified.
        for i, key in enumerate(notations):
            if time >= (notations[key] - 0.0001):  # Include a small (0.0001) floating-point rounding tolerance
                divisor = notations[key]
                notation = key
                break  # Exit the loop once the appropriate unit is found
        return f"{round(time / divisor)}{notation}"

    try:  # Manual unit scaling, if specified.
        return f"{round(time / notations[notation])}{notation}"
    except KeyError:
        print(f"ValueError: {notation} is not one of: {notations}")


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
    #           TODO: Check if StateFrag and VirtCompRatio values are not mangled by being indicated as type "size"
    zpool_keys = [("Name", "label"), ("LogicCapUsed", "size"), ("LogicCapFree", "size"), ("OpsRead", "size"), ("OpsWrite", "size"), ("BwRead", "size"), ("BwWrite", "size"), ("TotalwaitRead", "time"), ("TotalwaitWrite", "time"), ("DiskwaitRead", "time"), ("DiskwaitWrite", "time"), ("SyncqwaitRead", "time"), ("SyncqwaitWrite", "time"),
                  ("AsyncqwaitRead", "time"), ("AsyncqwaitWrite", "time"), ("ScrubWait", "time"), ("TrimWait", "time"), ("VirtCapUsed", "size"), ("VirtCapFree", "size"), ("VirtCompRatio", "size"), ("VirtCapUsedByChilds", "size"), ("VirtCapUsedBySnaps", "size"), ("StateHealth", "label"), ("StateFrag", "size"), ("StateText", "label")]

    zpool_vals = ["amalgm", "51567724367872", "16344298516480", "16", "0", "8468325", "0",
                  "15682379", "-", "15682379", "-", "3532", "-", "3510", "-", "-", "-"]
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
    # zpool.update({'VirtCapTot': zpool["VirtCapUsed"] + zpool["VirtCapFree"]})
    zpool.update({('VirtCapTot', 'size'): zpool[('VirtCapUsed', 'size')] + zpool[('VirtCapFree', 'size')]})
    
    zpool.update({('VirtCapUsedPerc', 'perc'): (zpool['VirtCapUsed', 'size'] / zpool['VirtCapTot', 'size']),
                   ('VirtCompPerc', 'perc'): zpool['VirtCompRatio', 'size'] - 1,
                   ('TotalwaitBoth', 'time'): zpool['TotalwaitRead', 'time'] + zpool['TotalwaitWrite', 'time']})
 
    # Sort the dictionary alphabetically by key
    zpool = dict(zpool.items())

    return zpool


# Get a raw dictionary of the latest stats
zpool = get_stats()

# Print the dictionary in a nice format
print_struct(zpool)
print(zpool)
