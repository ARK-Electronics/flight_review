# pylint: disable=import-error
"""Fresh, bounded process for expensive parsing; never forks the Bokeh server."""
import argparse
import os
import pickle
import sys


def limit_resources(memory_mb):
    """Limit address space on Linux (production). Threads cannot enforce this."""
    if sys.platform == 'linux':
        import resource  # pylint: disable=import-outside-toplevel
        maximum = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    """Parse one file and return the library object through a private temp file."""
    parser = argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('output')
    args = parser.parse_args()
    limit_resources(int(os.environ.get('PARSER_MEMORY_MB', '1536')))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'plot_app'))
    from helper import load_log_file_for_upload  # pylint: disable=import-outside-toplevel
    result = load_log_file_for_upload(args.input)
    with open(args.output, 'wb') as stream:
        pickle.dump(result, stream, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == '__main__':
    main()
