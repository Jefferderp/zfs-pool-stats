import curses
import time

def main(stdscr):
    # Clear screen
    stdscr.clear()

    # Enable scrolling
    stdscr.scrollok(True)

    for i in range(100):
        # Add string to stdscr
        stdscr.addstr(f"New line {i}\n")

        # Refresh the screen
        stdscr.refresh()

        # Sleep for a while
        time.sleep(0.1)

    # Wait for user input before closing
    stdscr.getch()

curses.wrapper(main)


# -------------------

def stdscr(stdscr, header, values):
    stdscr.clear()
    stdscr.addstr(0, 0, header)
    stdscr.refresh()

    # Enable scrolling
    stdscr.scrollok(True)

    for scr_row, value in enumerate(values, start=1):
        # If the window is full, scroll it up
        if scr_row >= stdscr.getmaxyx()[0]:
            stdscr.scroll()
            scr_row -= 1

        stdscr.addstr(scr_row, 0, f"{value}\n")
        stdscr.refresh()

curses.wrapper(stdscr, header, values)

# -----------

header = "*** Sticky Header ***"

def main(stdscr):
    stdscr.clear()
    stdscr.addstr(0, 0, header)  # Initial header
    stdscr.refresh()

    while True:
        new_output = get_new_data()  # Replace with your data generation logic

        stdscr.addstr("\n" * 2)       # Push previous content down
        stdscr.addstr(0, 0, header)  # Reprint header
        stdscr.addstr(1, 0, new_output)
        stdscr.refresh()

        time.sleep(0.5)  # Adjust delay as needed

def get_new_data():
    # Simulating new data for this example
    import random
    return f"Random Value: {random.randint(1,100)}"

curses.wrapper(main)