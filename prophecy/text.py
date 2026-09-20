"""Small helpers for sentences that have to agree with a number.

"3 file(s) import it" reads like a form. These give the form a person would
have written: "3 files import it", "1 file imports it".
"""


def plural(n, noun):
    """The count and the noun in the form the count needs: "1 file", "3 files"."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def pick(n, one, many):
    """The word for one thing or for several: pick(n, "imports", "import")."""
    return one if n == 1 else many
