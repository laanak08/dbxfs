# dbxfs

dbxfs allows you to mount your Dropbox folder as if it were a local
filesystem. It differs from the official Dropbox client in two main
ways:

* Internet connectivity is required for access
* No disk space is required for access, but will cache if disk space is available

dbxfs has been tested on Linux and macOS but it should run on any
POSIX system that provides a FUSE-compatible library or has the
ability to mount SMB shares. Windows support is coming very soon. It
runs on non-x86 architectures like ARM. It doesn't require a specific
file system.

It is written for Python 3.5+ and is licensed under the GPLv3.

Disclaimer: dbxfs is not affiliated with Dropbox, Inc.

# Usage

## (Optional) Install FUSE

macOS users can skip this. If you are on Linux, you must install your
OS's FUSE library. On Debian/Ubuntu, install the `libfuse2` package,
on Fedora install `fuse`.

## Install and Run dbxfs

    $ pip3 install dbxfs
    $ dbxfs <mount_point>

You can see the full list of command line options by passing `-h` to
the `dbxfs` command.

# Advanced Access Token Storage

By default dbxfs stores your access token in the system keyring or an
encrypted file but you may want to store it in a `gpg` encrypted file
or something else. To do that you must edit the dbxfs config
file. First you need to find the default config file location:

    $ dbxfs --print-default-config-file

It is a JSON encoded file. Add the following JSON key to the top-level
JSON object in that file:

    "access_token_command": ["gpg", "--decrypt", "/path/to/access/token/file.gpg"]

Adapt it to a decryption program of your choice. This configuration
works great for storing the access token using a OpenPGP card.

# Contact

Rian Hunter [@cejetvole](https://twitter.com/cejetvole)
