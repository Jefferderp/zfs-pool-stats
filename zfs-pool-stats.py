#! python3
import subprocess
import math
import argparse
import time
import curses
import random


""" TODO:
* When an invalid column is passed to --columns, exit with an error instead of ignoring silently.
* Add a third sub-argument to --columns for decimal places
* Add a fourth sub-argument to --columns for custom column names printed at runtime. Also include
  default column names which are more sane. Or maybe scrap this idea and use sane column names to begin with.
* Set default script parameters if no flags passed
* Shorten length of key names in {zpool_keys} in preparation for column output, where width is at a premium
* Strip whitespace from args.COLUMNS to prevent dictionary key mis-matches, causing failure to print.
* Standardize all input flags to lowercase handling
* Repeated output in aligned columns with automatic minimum width
* Upgrade conv_microseconds() to round up by length (99s = 1.65m != 100s), instead of time (59s > 1m)
Add to conv_bytes():
* Up/down/nearest rounding preference per-invocation or per-value
* A sticky header with ["stateHealth"] and ["stateText"]
* Use first line of `zpool iostat` (without -y) to get statistics since boot, and display as the last line (sticky)
   and with some special stylization (bold, underlined, etc.)
* An -c+ flag to add additional columns to the default set (a'la `lsblk`)
* Implement text coloring for pool state health
* A brief warning if the user specifies a REPEAT_DELAY < 1
* Restore shell_cmd() lines commented out and remove placeholder lists
* Change shell_cmd() to run locally instead of remotely
* Try removing the need for math module
* Implement threading or async so that any delay inherent to get_stats() isn't added to --interval
* Implement multiple simultaneous pool outputs
* Add internal ingestion delays of between 5 and 15 seconds (to save CPU) for any relatively stable values,
      which are extremely unlikely to change +/- 1%. These are:
      Name, LogicCapUsed, LogicCapFree, VirtCapUsed, VirtCapFree, VirtCompRatio,
      VirtCapUsedByChilds, VirtCapUsedBySnaps, StateHealth, StateFragPerc, StateText
"""


def parse_complex_arg(string):
    """Parse a flag argument in format 'Main:SubArg1:SubArg2' and intelligently split into a dictionary.
    Used by the parser module to handle complex/structured flags.

    Args:
        string: The raw flag (string) to be parsed and split.
                Primary arguments are split by ',' and Sub-arguments are split by ':'.

    Returns:
        A dictionary comprised of Primary arguments (as keys) and Sub-arguments (as a list of values).
        If no Sub-arguments were passed, then value is a list containing an empty string.
    """
    arguments = {}
    try:
        sub_args = string.split(',')  # Iterate over Primary arguments
        for i in sub_args:
            # If ':' separators, split Sub-arguments and stuff them into a list.
            if ':' in i:
                key, *value = i.split(':')
            # If no ':' separators, don't attempt to split non-existent Sub-arguments.
            else:
                key, value = i, None
            arguments[key] = value
        # Return a dictionary of Primary arguments (as keys) and Sub-arguments (as a list of values).
        return arguments
    except ValueError:
        raise argparse.ArgumentTypeError("ERROR: Invalid format for --columns. Use Column1,Column2, ... ")


###  Accept arguments  ###
# Define arguments parser
parser = argparse.ArgumentParser()

# Construct args.COLUMNS dictionary
parser.add_argument('--columns', '-c', dest="COLUMNS", type=parse_complex_arg,
                    # Default columns if none specified:
                    # TODO: VirtCapUsedPerc,VirtCompPerc are being ignored?
                    default="PoolName,VirtCapUsed,VirtCapFree,VirtCapTot,VirtCapUsedPerc,BwRead:M,BwWrite:M,TotalwaitBoth,StateFragPerc,VirtCompPerc,VirtCapUsedBySnaps:G",
                    help='A comma-separated list of columns to output. Optionally specify :scale. \
                          For example:  --columns PoolName,StateHealth,VirtCapFree:T ')

# Construct args.INTERVAL dictionary
parser.add_argument('--interval', '-t', dest="INTERVAL", type=float,
                    default=1.0,  # Default delay interval of 1 second, if not specified.
                    help='The frequency of time (in seconds) to output statistics. Accepts whole or decimal numbers. \
                          This also affects the sampling of some delay measurements; the recommendation is 1 second \
                          or more to allow a sufficient sampling window for collecting i/o timing statistics. \
                          For example:  --interval 1.5 ')

# Construct args.POOL dictionary
parser.add_argument('--pool', '-p', dest="POOL", type=str,
                    default="amalgm",  # TODO: Retrieve default args.POOL name intelligently from the system...somehow.
                    help='The name of the pool to report statistics for. For example:  --pool tank ')

args = parser.parse_args()  # Expose args.COLUMNS, args.INTERVAL, etc. for use


###  Define functions  ###


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
    """Try to convert string to float, otherwise pass through original input.

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


def conv_bytes(bytes, notation=None):
    """Convert byte values to a specified notation. Uses powers of 1024 as output by `zfs get`.

    Args:
        bytes: The byte value (int or float) to convert.
        notation: The desired notation ('B', 'K', 'M', 'G', 'T', 'P', 'E').
            If unspecified, automatically chooses the highest notation.

    Returns:
        A string representing the byte value expressed in the chosen notation."""
    # Handle 0 and strings. Return them unmodified.
    if bytes == 0 or isinstance(bytes, str):
        return bytes

    notations = ("B", "K", "M", "G", "T", "P", "E")

    if notation is None:  # Automatic unit scaling, if not specified.
        i = int(math.floor(math.log(bytes, 1024)))  # Math, how does it work?!
        p = math.pow(1024, i)
        return f"{round(bytes / p)}{notations[i]}"

    try:  # Manual unit scaling, if specified.
        notation = str(notation[0])
        index = notations.index(notation.upper())  # Find index of target notation
        divisor = 1024 ** index  # Calculate byte value to divide by
        return f"{round(bytes / divisor)}{notation}"
    except ValueError:
        print(f"ValueError: {notation} is not one of: {notations}")


def conv_str(input, notation=None):
    """Accepts any valid input and returns a string.
    This function is used instead of the built-in str() because str()
    will error when receiving an invalid second parameter.

    Args:
        input: The input value to convert to a string.
        notation: Does nothing. Exists for compatibility reasons.

    Returns:
        input formatted as a string.
    """

    return str(input)


def conv_perc(input, notation=None):
    """Accepts any valid input and returns a percentage value as a string.

    Args:
        input: The input value to convert to a string.
        notation: Does nothing. Exists for compatibility reasons.

    Returns:
        input formatted as a string, with a '%' appended.
    """

    # return str(f"{(input)}%")
    # return round(input, 2)
    # Return a string with no leading/trailing decimals, and append a '%'
    return f"{input:.0%}"


def conv_microseconds(microseconds, notation=None):
    """Convert microsecond values to a specified notation. Uses microseconds to align with `zpool iostat -p`.

    Args:
        microseconds: The microsecond value (int or float) to convert.
        notation: The desired notation ('d', 'h', 'm', 's', 'ms', 'us').
            If unspecified, automatically chooses the highest notation.

    Returns:
        A string representing the time value expressed in the chosen notation."""
    # Handle 0 and strings. Return them unmodified.
    if microseconds == 0 or isinstance(microseconds, str):
        return microseconds

    notations = {"d": 86400000000, "h": 3600000000, "m": 60000000, "s": 1000000, "ms": 1000, "us": 1}

    if notation is None:  # Automatic unit scaling, if not specified.
        for i, key in enumerate(notations):
            if microseconds >= (notations[key] - 0.0001):  # Subtract a small rounding tolerance
                divisor = notations[key]
                notation = key
                break  # Exit the loop once the appropriate unit is found
        return f"{round(microseconds / divisor)}{notation}"

    try:  # Manual unit scaling, if specified.
        notation = str(notation[0])
        return f"{round(microseconds / notations[notation])}{notation}"
    except KeyError:
        print(f"ValueError: {notation} is not one of: {notations}")


def get_stats(pool):
    """Ingest ZFS pool statistics from `iostat`, `zfs get` and `zpool status` system commands.
    Args:
        pool: The name of the ZFS pool to collect statistics on.

    Returns:
        A dictionary of ZFS pool statistics, formatted as floats or strings."""

    # NOTE: In case the output sequence from any of these underlying commands ever changes in a future version,
    #       the keys and values in this dictionary will be misaligned, requiring source code adjustment.
    #       Starting from ["Name"], the values of `zpool iostat` are assigned. Starting from ["VirtCapUsed"], the values of `zfs get` are assigned.
    #       Starting from ["StateHealth"], the values of `zfs get` (again) are assigned. Starting from ["StateText"], the values of `zpool status` are assigned.
    zpool_keys = [("PoolName", "label"), ("LogicCapUsed", "size"), ("LogicCapFree", "size"), ("OpsRead", "size"), ("OpsWrite", "size"), ("BwRead", "size"), ("BwWrite", "size"), ("TotalwaitRead", "time"), ("TotalwaitWrite", "time"), ("DiskwaitRead", "time"), ("DiskwaitWrite", "time"), ("SyncqwaitRead", "time"), ("SyncqwaitWrite", "time"),
                  ("AsyncqwaitRead", "time"), ("AsyncqwaitWrite", "time"), ("ScrubWait", "time"), ("TrimWait", "time"), ("VirtCapUsed", "size"), ("VirtCapFree", "size"), ("VirtCompRatio", "label"), ("VirtCapUsedByChilds", "size"), ("VirtCapUsedBySnaps", "size"), ("StateHealth", "label"), ("StateFragPerc", "perc"), ("StateText", "label")]

    # Map zpool_keys dict to different functions, for later use.
    # This allows us to intelligently convert to higher notations by
    # constructing the key name as a tuple, with the first value in the
    # tuple being the key name, and the second value being the type.
    global zpool_keys_types
    global zpool_keys_map
    zpool_keys_types = ('size', 'time', 'label', 'perc')
    zpool_keys_map = {'size': conv_bytes, 'time': conv_microseconds, 'label': conv_str, 'perc': conv_perc}

    zpool_vals = ["amalgm", "51567724367872", "16344298516480", "16", "0", "8468325", "0",
                  "15682379", "-", "15682379", "-", "3532", "-", "3510", "-", "-", "-"]
    # shell_cmd("zpool iostat -Hypl " + pool + " " + REPEAT_DELAY + " " + "1").split()

    zpool_vals.extend(["54866186481664", "12908397449216", "1.01", "54700434006016"])
    # shell_cmd("zfs get used,available,compressratio,usedbychildren " + pool + " -Hp -d 0 -o value | tr '\n' ' '").split()

    # TODO: This `zfs get usedbysnapshots` command is very slow, because it's recursively checking
    #       all snapshots sizes (`-r`) and summing them (`awk`) before returning. Alternative?
    #       Also, this method with `grep` and `awk` is very clunky and may break with future `zfs` versions.
    #       Parsing and addition should be performed locally.
    zpool_vals.extend(["1381425606656"])
    # shell_cmd("zfs get usedbysnapshots " + pool + " -Hp -r -o value | grep -v '-' | awk '{s+=$1} END {printf \"%.0f\", s}'").split())

    zpool_vals.extend(["ONLINE", "20%"])
    # shell_cmd("zpool list -H -o health,frag " + pool)

    # TODO: Clean up this `zpool status` command and perform text parsing locally instead.
    zpool_vals.extend(
        ["scan: scrub repaired 0B in 1 days 12:59:37 with 0 errors on Sat Jan 27 22:59:39 2024 remove: Removal of mirror canceled on Tue Jan  9 08:30:58 2024"])
    # shell_cmd("zpool status " + pool + " | sed -n '3,$p' | tr '\n' ' ' | tr -d '\011\012' | sed -e 's/^[ \t]*//' | " + "sed --regexp-extended 's/ config\:.*//g'")

    # Merge keys and values lists into a dictionary.
    zpool = dict(zip(zpool_keys, zpool_vals))

    # Convert all eligible values to floats, so we can do math.
    zpool = {key: conv_float(value) for key, value in zpool.items()}

    # Create some more dictionary entries
    # Correctly reference keys by their full tuple names
    zpool.update({('VirtCapTot', 'size'): zpool[('VirtCapUsed', 'size')] + zpool[('VirtCapFree', 'size')]})
    zpool.update({('VirtCapUsedPerc', 'perc'): (zpool['VirtCapUsed', 'size'] / zpool['VirtCapTot', 'size']),
                  ('VirtCompPerc', 'perc'): zpool['VirtCompRatio', 'label'] - 1,
                  ('TotalwaitBoth', 'time'): zpool['TotalwaitRead', 'time'] + zpool['TotalwaitWrite', 'time'],
                  ('StateFragPerc', 'perc'): zpool['StateFragPerc', 'perc'] * 0.01})

    return zpool


def conv_dict_notation(ref_keys, conv_keys):
    """Convert the values in a dictionary from raw integer/time values to human-readable notation.

    Args:
        ref_keys:  A dictionary containing raw values to be converted to human-readable notation.
                   Dictionary keys must be in a nested tuple format, as returned by get_stats().
        conv_keys: A dictionary containing keys to be matched against {ref_keys}. Keys found in
                   both dictionaries will have their corresponding values in {ref_keys} converted.
                   Keys found in both dictionaries will have their values extracted from {ref_keys},
                   converted to human-readable notation, and returned as a new dictionary.

    Returns:
        A dictionary of keys common to both {ref_keys} and {conv_keys}, with values converted."""

    output = {}

    for key, notation in conv_keys.items():
        # Try all possible permutations of key_match in {ref_keys}.
        #   NOTE: This is because each key of {ref_keys} is a tuple of (key_match, key_type) but
        #   we want to access the key by just key_match, hence this crutch. Maybe we could
        #   specify key_type in zpool_vals instead of zpool_keys, however this may be impractical.
        for key_type in zpool_keys_types:  # Try all key_type to match against keys in {ref_keys}
            key_match = (key, key_type)  # Construct a tuple to properly match key_match against {ref_keys}
            if key_match in ref_keys:  # Proceed once the correct match for key_match has been found
                # Check which function to use for conversion
                # zpool_keys_map is defined in get_stats()
                key_use_func = zpool_keys_map.get(key_match[1])

                # Calculate notation and append the key from {conv_keys} to {output}:
                output.update({key: key_use_func(ref_keys[key_match], notation)})

                # Print everything for debugging
                # print(f"{key} : {notation[0]} : {ref_keys[key_match]} : {key_use_func} : {key_use_func(ref_keys[key_match])}")
                # print(f"ref_keys: {ref_keys}")

    return (output)


def get_keys_width(input_dict):
    """For each key and value pair in input_dict, calculate the maximum length of both. Return a new dictionary.

    Args:
        input_dict: The input dictionary to perform length calculations on.

    Returns:
        A new dictionary. Keys are identical to those in input_dict. Values are integers indicating
        the maximum length in characters of each (key, value) of input_dict, whichever was greater."""
    column_widths = {}

    # Construct a dictionary, where:
    # keys = same as {input_dict}
    # values = the maximum string length of each (key and value) in {input_dict}
    for key, value in input_dict.items():
        max_width = max(len(str(key)), len(str(value)))
        column_widths[key] = max_width + 2
    return column_widths


def print_columns(input_dict=conv_dict_notation(get_stats(args.POOL), args.COLUMNS), interval=args.INTERVAL):
    """On a loop, print out a dictionary in columns format.

    Args:
        input_dict: The input dictionary to be output at each interval.
        interval: The delay in seconds (float) between outputs.
    """

    # Determine the minimum width of each column.
    column_widths = get_keys_width(input_dict)

    # Output in aligned columns, as specified in {column_widths} from calc_column_widths()

    # Assemble header column as combined strings
    header = ""
    for key in input_dict:
        header += f"{key:<{column_widths[key]}}"

    # Assemble values column as combined strings
    values = ""
    for key, value in input_dict.items():
        values += f"{value:<{column_widths[key]}}"

    # Cast some curses
    def stdscr(stdscr, header, values):
        stdscr.clear()  # Clear the screen
        stdscr.scrollok(True)  # Enable scrolling
        curses.echo()  # Echo stdin

        # Define curses scrolling region
        scr_height, scr_width = stdscr.getmaxyx()  # Get screen dimensions
        scr_row = 0  # Set first row for values to begin printing on

        # Print header initially
        stdscr.addstr(1, 0, header + "\n")  # Print columns header on first line

        while True:
            # Keep track of screen height to avoid errors with printing out of bounds
            scr_row += 1
            if scr_row >= scr_height:
                scr_row = scr_height - 1

            stdscr.addstr(1, 0, header + "\n")  # Repeatedly print columns header on first line
            # TODO: Remove debug random.randint() value
            stdscr.addstr(scr_row, 0, f"{values} {random.randint(100, 999)} \n")  # Repeatedly print latest values
            stdscr.refresh()

            time.sleep(interval)

    # Call stdscr() sub-function
    curses.wrapper(stdscr, header, values)


###  Run output  ###
try:
    # raw_stats = get_stats(args.POOL)
    # converted_keys = convert_keys(ref_keys=raw_stats, conv_keys=args.COLUMNS)
    # print_columns(converted_keys, args.INTERVAL)

    print_columns()

except KeyboardInterrupt:  # Exit gracefully on ^C (SIGINT)
    exit
