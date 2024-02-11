#! /bin/bash
# Requires `bash` `coreutils` `bc`

##############
#### TODO ####
# Script is not exiting properly on ^C (SIGINT). Processes continue running in their own shell, regardless of parent shell's alive status.
##############

# ZFS splits its reporting between `zfs` and `zpool` commands - and between sub-commands, at that.
# Also, `zpool stats` does not offer the ability to specify output fields/columns, so...I'll do it myself.

### Define constants.
# Specify name of pool to report on.
POOL_NAME="amalgm"
# Specify a constant "random" number that will act as essentially a session ID.
CONST_RANDOM=$RANDOM
# Specify a named pipe (fifo) for storage.
WORKING_FIFO="/tmp/.zfs-stats-$CONST_RANDOM.fifo"
# Specify the delay in seconds between stat collections, and therefore reports.
# Lower values (less than a second) are subject to sampling errors. Values such as bandwidth may be
# under-reported if `zpool iostat` is not given enough time to observe the pool. This may be resolvable
# in the future by running `zpool iostat` in a background loop, and observing it periodically.
# Note there's an additional ~80ms delay per loop due to overhead from other commands contained within.
REPEAT_DELAY=9.9

	######################################################

### Some very useful text functions to be seen later.

# Wrap the printing of an integer byte value in this function to convert it to human-readable 'powers-of' abbreviated units (e.g. 20M, 1G, 5T).
# $1 should be [d (decimal), b (binary), K (decimal Kilobytes), M (decimal Megabytes), G (decimal Gigabytes), T (decimal Terabytes)].
# $2 should be the number of decimals desired.
# $3 should be the desired method of rounding, one of [up, down, from-zero, towards-zero, nearest].
# $4 should be the input number to re-format.
convBytesH() {
	  if [[ $1 = "d" ]]; then numFmt='--to=si';
	elif [[ $1 = "b" ]]; then numFmt='--to=iec-i';
	elif [[ $1 = "K" ]]; then numFmt='--to-unit=K --suffix=K';
	elif [[ $1 = "M" ]]; then numFmt='--to-unit=M --suffix=M';
	elif [[ $1 = "G" ]]; then numFmt='--to-unit=G --suffix=G';
	elif [[ $1 = "T" ]]; then numFmt='--to-unit=T --suffix=T';
	fi;
	echo $(printf "$4" | numfmt --from=none --format='%.'$2'f' --round=$3 $numFmt)
}

# Wrap the printing of a time value in this function to convert the value to milli-seconds.
convMs() { printf $(bc <<< "$1 / 1000000") ; }

# Wrap the printing of any integer value in this function to re-format the integer with two leading zeroes.
padLeadingZeroes() { printf '%02d' "$1" ; }

# Cut short the printing of any string to the current terminal column width, to prevent line wrapping.
noLineWrap() { printf "$@" | cut -c 1-$(tput cols) ; }

# The function to be called by our exit trap (seen later).
exitCleanup() { rm "$WORKING_FIFO" ; line format clear ; exit ; }

# A shorthand function for printing standard ANSI terminal sequences that offer cursor navigation and format control.
# Thanks to:
# https://tldp.org/HOWTO/Bash-Prompt-HOWTO/x361.html
# https://www.lihaoyi.com/post/BuildyourownCommandLinewithANSIescapecodes.html#16-colors
# https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
line() {
	  if [[ "$1" == "up" ]]; then printf "\033["$2"A" # Example: `line up 5`
	elif [[ "$1" == "down" ]]; then printf "\033["$2"B"
	elif [[ "$1" == "left" ]]; then printf "\033["$2"D" # Example: `line left 3`
	elif [[ "$1" == "right" ]]; then printf "\033["$2"C"
	elif [[ "$1" == "start" ]]; then printf "\033[G" # Go to first column of line
	elif [[ "$1" == "top" ]]; then printf "\033[0;0H" # Go to top-most visible line
	elif [[ "$1" == "bottom" ]]; then printf "\033[500;0H" # Go to bottom-most visible line
	elif [[ "$1" == "clear" ]]; then printf "\033[K" # Clear the current line's contents
	elif [[ "$1" == "fill" ]]; then columns=$(tput cols); for ((i=0; i<columns; i++)); do printf " "; done; printf "\033[G" # Get the current X number of columns, and print X blank characters. Then move cursor to start of line.
	elif [[ "$1" == "return" ]]; then printf "\n" # Go to next line
	elif [[ "$1" == "goto" ]]; then printf "\033["$3";"$4"H"
	elif [[ "$1" == "remember" ]]; then printf "\033[s" # Save and restore is not always supported. Raw TTY terminals (over SSH or otherwise) always offer support.
	elif [[ "$1" == "recall" ]]; then printf "\033[u"
	elif [[ "$1" == "format" ]]; then
		  if [[ "$2" == "bold" ]]; then printf "\033[1m"
		elif [[ "$2" == "underline" ]]; then printf "\033[4m"
		elif [[ "$2" == "inverted" ]]; then printf "\033[7m"
		elif [[ "$2" == "clear" ]]; then printf "\033[0m" # Clear the current line format (reset to default).
		elif [[ "$2" == "bgcolor" ]]; then
				  if [[ "$3" == "white" ]]; then printf "\033[47m"
				elif [[ "$3" == "red" ]]; then printf "\033[41m"
				elif [[ "$3" == "green" ]]; then printf "\033[42m"
				elif [[ "$3" == "yellow" ]]; then printf "\033[43m"
				elif [[ "$3" == "blue" ]]; then printf "\033[44m"
				elif [[ "$3" == "magenta" ]]; then printf "\033[45m"
				elif [[ "$3" == "cyan" ]]; then printf "\033[46m"
				elif [[ "$3" == "black" ]]; then printf "\033[40m"
				elif [[ "$3" == "grey" ]]; then printf "\u001b[40;1m"
				elif [[ "$3" == "salmon" ]]; then printf "\u001b[41;1m"
				elif [[ "$3" == "lightgreen" ]]; then printf "\u001b[42;1m"
				elif [[ "$3" == "lightyellow" ]]; then printf "\u001b[43;1m"
				elif [[ "$3" == "purple" ]]; then printf "\u001b[44;1m"
				elif [[ "$3" == "lightpurple" ]]; then printf "\u001b[45;1m"
				elif [[ "$3" == "lightgrey" ]]; then printf "\u001b[47;1m"
				  fi
		elif [[ "$2" == "fgcolor" ]]; then
				    if [[ "$3" == "white" ]]; then printf "\u001b[37m"
				  elif [[ "$3" == "red" ]]; then printf "\u001b[31m"
				  elif [[ "$3" == "green" ]]; then printf "\u001b[32m"
				  elif [[ "$3" == "yellow" ]]; then printf "\u001b[33m"
				  elif [[ "$3" == "blue" ]]; then printf "\u001b[34m"
				  elif [[ "$3" == "pink" ]]; then printf "\u001b[35m"
				  elif [[ "$3" == "cyan" ]]; then printf "\u001b[36m"
				  elif [[ "$3" == "black" ]]; then printf "\u001b[30m"
				  elif [[ "$3" == "grey" ]]; then printf "\u001b[30;1m"
				  elif [[ "$3" == "salmon" ]]; then printf "\u001b[31;1m"
				  elif [[ "$3" == "lightgreen" ]]; then printf "\u001b[32;1m"
				  elif [[ "$3" == "lightyellow" ]]; then printf "\u001b[33;1m"
				  elif [[ "$3" == "purple" ]]; then printf "\u001b[34;1m"
				  elif [[ "$3" == "lightpurple" ]]; then printf "\u001b[35;1m"
				  elif [[ "$3" == "lightgrey" ]]; then printf "\u001b[37;1m"
				    fi
		   fi
	  fi
}

### Prepare fifo for use.
test -p "$WORKING_FIFO" || mkfifo "$WORKING_FIFO"
printf ' ' >> "$WORKING_FIFO" &
cat "$WORKING_FIFO" >> /dev/null

collectStats() {
	### Collect and catalogue stats from `zpool iostat`.
	# Child shells in Bash (Bourne-like shells) cannot escalate variables to their parent shell. If they could, there would be less suffering in the world. And in this script. The workaround is a temporary file, which can later be re-read by the parent process.
	while IFS='	' read -r poolName poolCapacityAllocatedLogical poolCapacityFreeLogical poolOperationsRead poolOperationsWrite poolBandwidthRead poolBandwidthWrite poolTotalWaitRead poolTotalWaitWrite poolDiskWaitRead poolDiskWaitWrite poolSyncqWaitRead poolSyncqWaitWrite poolAsyncqWaitRead poolAsyncqWaitWrite poolScrubWait poolTrimWait undefined;
		do printf "poolName=$poolName \npoolCapacityAllocatedLogical=$poolCapacityAllocatedLogical \npoolCapacityFreeLogical=$poolCapacityFreeLogical \npoolOperationsRead=$poolOperationsRead \npoolOperationsWrite=$poolOperationsWrite \npoolBandwidthRead=$poolBandwidthRead \npoolBandwidthWrite=$poolBandwidthWrite \npoolTotalWaitRead=$poolTotalWaitRead \npoolTotalWaitWrite=$poolTotalWaitWrite \npoolDiskWaitRead=$poolDiskWaitRead \npoolDiskWaitWrite=$poolDiskWaitWrite \npoolSyncqWaitRead=$poolSyncqWaitRead \npoolSyncqWaitWrite=$poolSyncqWaitWrite \npoolAsyncqWaitRead=$poolAsyncqWaitRead \npoolAsyncqWaitWrite=$poolAsyncqWaitWrite \npoolScrubWait=$poolScrubWait \npoolTrimWait=$poolTrimWait \n"
	done <<< $(zpool iostat $POOL_NAME $REPEAT_DELAY 1 -Hypl) >> "$WORKING_FIFO" &

	# Source results of first lookup.
	source "$WORKING_FIFO"

	### Collect and catalogue stats from `zfs get`.
 	while IFS='	' read -r poolCapacityAllocatedVirtual poolCapacityFreeVirtual poolCapacityCompressPercent poolCapacityAllocatedBychildren undefined;
		do printf "poolCapacityAllocatedVirtual=$poolCapacityAllocatedVirtual \npoolCapacityFreeVirtual=$poolCapacityFreeVirtual \npoolCapacityCompressPercent=$poolCapacityCompressPercent \npoolCapacityAllocatedBychildren=$poolCapacityAllocatedBychildren \n"
	done <<< $(zfs get used,available,compressratio,usedbychildren $POOL_NAME -Hp -d 0 -o value | tr '\n' '	') >> "$WORKING_FIFO" &

	# Source results of second lookup. For some reason, each input requires its own read operation.
	source "$WORKING_FIFO"

	### Collect and catalogue 'usedbysnapshots' from `zfs get`.
 	while IFS='	' read -r poolCapacityAllocatedBysnapshots undefined;
		do printf "poolCapacityAllocatedBysnapshots=$poolCapacityAllocatedBysnapshots \n"
	done <<< $(zfs get usedbysnapshots amalgm -Hp -r   -o value | grep -v '-' | awk '{s+=$1} END {printf "%.0f", s}') >> "$WORKING_FIFO" &

	# Source results of third lookup.
	source "$WORKING_FIFO"

	### Collect pool health state.
	while IFS=$'\t' read -r poolStateHealth poolStateFragmentation;
		do echo -ne "poolStateHealth=$poolStateHealth \npoolStateFragmentation=$poolStateFragmentation \n" | sed 's/%//';
	done <<< $(zpool list -H -o health,frag $POOL_NAME) >> "$WORKING_FIFO" &

	# Source results of fourth lookup.
	source "$WORKING_FIFO"

	### Collect pool status texts.
	while IFS='     ' read -r poolStateDescription;
		do printf "poolStateDescription=\"$poolStateDescription\" \n" ;
	done <<< $( zpool status $POOL_NAME | sed -n '3,$p' | tr '\n' ' ' | tr -d '\011\012' | sed -e 's/^[ \t]*//' | sed --regexp-extended 's/ config\:.*//g' ) >> "$WORKING_FIFO" &

	# Source results of fourth lookup.
	source "$WORKING_FIFO"

	### Cleanup some variables. Generate others.
	poolCapacityTotalVirtual=$(bc <<< "$poolCapacityAllocatedVirtual + $poolCapacityFreeVirtual")
	poolCapacityAllocatedPercentage=$(bc <<< "scale=2; x = $poolCapacityAllocatedVirtual / $poolCapacityTotalVirtual * 100; scale = 0; x / 1")
	poolCapacityCompressRatio=$(bc <<< "x = $poolCapacityCompressPercent * 100 - 100; 100 - x / 1")
	poolCapacityCompressPercent=$(bc <<< "x = $poolCapacityCompressPercent - 1; x * 100 / 1")
	poolTotalWaitRead="${poolTotalWaitRead//\-}" ; poolTotalWaitRead=$[poolTotalWaitRead+1]
	poolTotalWaitWrite="${poolTotalWaitWrite//\-}" ; poolTotalWaitWrite=$[poolTotalWaitWrite+1]
	poolTotalWaitNet=$(bc <<< "$poolTotalWaitRead + $poolTotalWaitWrite")
	[ $poolStateHealth = "ONLINE" ] && poolStateHealthColor="lightgreen" || poolStateHealthColor="salmon"

trap exitCleanup INT # The exit trap has to go here, since the script is always waiting for this loop to complete.

} # End function collectStats() definition.

### Print statistics.
printStats() {
	# printHeader() { printf "%5s %5s %6s %5s %7s %7s %5s %5s %9s %8s %8s" "used" "free" "capac" "perc" "read" "write" "frag" "comp" "useChild" "useSnap" "totWait" ; }
	printHeader() { printf "%5s %5s %6s %5s %7s %7s %5s %5s %8s" "used" "free" "capac" "perc" "read" "write" "frag" "comp" "useSnap" ; }

	printSubheader() { printf " zpool $poolName is $poolStateHealth: $poolStateDescription" ; }

	# printStats() {  printf "\n%5s %5s %6s %5s %7s %7s %5s %5s %9s %8s %8s" "$(convBytesH d 1 nearest $poolCapacityAllocatedVirtual)" "$(convBytesH d 1 from-zero $poolCapacityFreeVirtual)" "$(convBytesH d 1 nearest $poolCapacityTotalVirtual)" "$poolCapacityAllocatedPercentage%" "$(convBytesH M 1 nearest $poolBandwidthRead)" "$(convBytesH M 1 nearest $poolBandwidthWrite)" "$poolStateFragmentation%" "$poolCapacityCompressPercent%" "$(convBytesH d 1 nearest $poolCapacityAllocatedBychildren)" "$(convBytesH d 1 nearest $poolCapacityAllocatedBysnapshots)" "$(convMs $poolTotalWaitNet)ms" ; }
	printStats() {  printf "\n%5s %5s %6s %5s %7s %7s %5s %5s %8s" "$(convBytesH d 1 nearest $poolCapacityAllocatedVirtual)" "$(convBytesH d 1 towards-zero $poolCapacityFreeVirtual)" "$(convBytesH d 1 nearest $poolCapacityTotalVirtual)" "$poolCapacityAllocatedPercentage%" "$(convBytesH M 1 up $poolBandwidthRead)" "$(convBytesH M 1 up $poolBandwidthWrite)" "$poolStateFragmentation%" "$poolCapacityCompressPercent%" "$(convBytesH d 1 nearest $poolCapacityAllocatedBysnapshots)" ; }

	printMainLoop() {
		printStats 			# Print stats.
		line remember 			# Remember this line, we'll be printing stats to it next.
			# line format bold; line format bgcolor lightgrey; line format fgcolor black 	# Format the text output bold, colored black on grey.
			# line format bold; line format fgcolor $poolStateHealthColor	# Format the text output bold, colored based on $poolStateHealth.
		line format fgcolor $poolStateHealthColor	# Format the text color based on $poolStateHealth.
		line top			# Go to the first line.
		line fill			# Print whitespace of the same length as the current terminal columns width.
		printSubheader			# Print sub-header.
		line fill			# Print whitespace of the same length as the current terminal columns width.
		line fill			# Print whitespace of the same length as the current terminal columns width.
		line format fgcolor cyan
		printHeader			# Print header.
		line recall 			# Go to the line we remembered earlier.
		line format clear 		# Restore default text format.
		collectStats 			# Collect stats in preparation for next run.
	}

	# Begin printing stats in an infinite loop.
	clear
	collectStats
	while : ; do printMainLoop; done

} # End function printStats() definition.

printStats
