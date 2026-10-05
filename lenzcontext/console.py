"""Console argument messages use stdout, including usage errors."""

import argparse
import sys


class ArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        super()._print_message(message, sys.stdout)
