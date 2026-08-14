#!/usr/bin/env python3
"""Print bold, monochrome, block-logo text from 5 to 12 lines tall."""

from __future__ import annotations

import argparse


FONT = {
    "A": (" ### ", "#   #", "#####", "#   #", "#   #"),
    "B": ("#### ", "#   #", "#### ", "#   #", "#### "),
    "C": (" ####", "#    ", "#    ", "#    ", " ####"),
    "D": ("#### ", "#   #", "#   #", "#   #", "#### "),
    "E": ("#####", "#    ", "#### ", "#    ", "#####"),
    "F": ("#####", "#    ", "#### ", "#    ", "#    "),
    "G": (" ####", "#    ", "# ###", "#   #", " ####"),
    "H": ("#   #", "#   #", "#####", "#   #", "#   #"),
    "I": ("#####", "  #  ", "  #  ", "  #  ", "#####"),
    "J": ("#####", "    #", "    #", "#   #", " ### "),
    "K": ("#   #", "#  # ", "###  ", "#  # ", "#   #"),
    "L": ("#    ", "#    ", "#    ", "#    ", "#####"),
    "M": ("#   #", "## ##", "# # #", "#   #", "#   #"),
    "N": ("#   #", "##  #", "# # #", "#  ##", "#   #"),
    "O": (" ### ", "#   #", "#   #", "#   #", " ### "),
    "P": ("#### ", "#   #", "#### ", "#    ", "#    "),
    "Q": (" ### ", "#   #", "#   #", "#  ##", " ####"),
    "R": ("#### ", "#   #", "#### ", "#  # ", "#   #"),
    "S": (" ####", "#    ", " ### ", "    #", "#### "),
    "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  "),
    "U": ("#   #", "#   #", "#   #", "#   #", " ### "),
    "V": ("#   #", "#   #", "#   #", " # # ", "  #  "),
    "W": ("#   #", "#   #", "# # #", "## ##", "#   #"),
    "X": ("#   #", " # # ", "  #  ", " # # ", "#   #"),
    "Y": ("#   #", " # # ", "  #  ", "  #  ", "  #  "),
    "Z": ("#####", "   # ", "  #  ", " #   ", "#####"),
    "0": (" ### ", "#  ##", "# # #", "##  #", " ### "),
    "1": ("  #  ", " ##  ", "  #  ", "  #  ", "#####"),
    "2": (" ### ", "#   #", "   # ", "  #  ", "#####"),
    "3": ("#### ", "    #", " ### ", "    #", "#### "),
    "4": ("#   #", "#   #", "#####", "    #", "    #"),
    "5": ("#####", "#    ", "#### ", "    #", "#### "),
    "6": (" ### ", "#    ", "#### ", "#   #", " ### "),
    "7": ("#####", "    #", "   # ", "  #  ", "  #  "),
    "8": (" ### ", "#   #", " ### ", "#   #", " ### "),
    "9": (" ### ", "#   #", " ####", "    #", " ### "),
    " ": ("   ", "   ", "   ", "   ", "   "),
    "-": ("     ", "     ", "#####", "     ", "     "),
    "_": ("     ", "     ", "     ", "     ", "#####"),
    ".": (" ", " ", " ", " ", "#"),
    ":": (" ", "#", " ", "#", " "),
    "!": ("#", "#", "#", " ", "#"),
    "?": ("### ", "   #", " ## ", "    ", " #  "),
}


def _shadow_character(mask: list[list[bool]], row: int, column: int) -> str:
    """Choose a box-drawing character for one exposed edge of a mask."""
    rows, columns = len(mask), len(mask[0])

    def filled(y: int, x: int) -> bool:
        return 0 <= y < rows and 0 <= x < columns and mask[y][x]

    north = filled(row - 1, column)
    south = filled(row + 1, column)
    west = filled(row, column - 1)
    east = filled(row, column + 1)
    if not south and not east:
        return "╝"
    if not north and not east:
        return "╗"
    if not south and not west:
        return "╚"
    if not north and not west:
        return "╔"
    if not east or not west:
        return "║"
    if not north or not south:
        return "═"
    return "·"


def make_banner(text: str, height: int = 6, depth: int = 1) -> str:
    """Return *text* as solid block letters with offset outline echoes."""
    if not 5 <= height <= 12:
        raise ValueError("height must be from 5 to 12")
    if not 0 <= depth <= 3:
        raise ValueError("depth must be from 0 to 3")

    # Always leave at least five rows for the face of the letters.
    depth = min(depth, height - 5)
    face_height = height - depth

    glyphs = [FONT.get(letter, FONT["?"]) for letter in text.upper()]
    source_lines = ["  ".join(glyph[row] for glyph in glyphs).rstrip() for row in range(5)]
    source_width = max((len(line) for line in source_lines), default=0)
    source_lines = [line.ljust(source_width) for line in source_lines]

    # Terminal characters are roughly twice as tall as they are wide, so each
    # font pixel becomes two horizontal cells to keep the logo proportional.
    face: list[list[bool]] = []
    for output_row in range(face_height):
        source_row = round(output_row * 4 / max(face_height - 1, 1))
        expanded = [pixel == "#" for pixel in source_lines[source_row] for _ in range(2)]
        face.append(expanded)

    canvas_width = (len(face[0]) if face else 0) + depth
    canvas = [[" " for _ in range(canvas_width)] for _ in range(height)]

    # Paint the farthest echo first, then the nearer ones, then the solid face.
    for offset in range(depth, 0, -1):
        for row, mask_row in enumerate(face):
            for column, is_filled in enumerate(mask_row):
                if not is_filled:
                    continue
                north = row > 0 and face[row - 1][column]
                south = row + 1 < len(face) and face[row + 1][column]
                west = column > 0 and mask_row[column - 1]
                east = column + 1 < len(mask_row) and mask_row[column + 1]
                if north and south and west and east:
                    continue
                canvas[row + offset][column + offset] = _shadow_character(face, row, column)

    for row, mask_row in enumerate(face):
        for column, is_filled in enumerate(mask_row):
            if is_filled:
                canvas[row][column] = "█"

    return "\n".join("".join(line).rstrip() for line in canvas)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="*", help="text to turn into a banner")
    parser.add_argument(
        "--height", type=int, default=6, metavar="5-12", help="total banner height (default: 6)"
    )
    parser.add_argument(
        "--depth", type=int, default=1, choices=range(4), help="outline echoes, 0-3 (default: 1)"
    )
    args = parser.parse_args()

    text = " ".join(args.text) if args.text else input("Banner text: ")
    try:
        print(make_banner(text, args.height, args.depth))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
