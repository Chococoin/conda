# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

import hashlib
from logging import DEBUG, getLogger
from os.path import basename, exists, join
import tempfile
import warnings
import sys
import os
import ctypes
from ctypes.util import find_library

from . import (ConnectionError, HTTPError, InsecureRequestWarning, InvalidSchema,
               SSLError, RequestsProxyError)
from .session import CondaSession
from ..disk.delete import rm_rf
from ... import CondaError
from ..._vendor.auxlib.ish import dals
from ..._vendor.auxlib.logz import stringify
from ...base.context import context
from ...common.compat import text_type
from ...common.io import time_recorder
from ...exceptions import (BasicClobberError, CondaDependencyError, CondaHTTPError,
                           ChecksumMismatchError, maybe_raise, ProxyError)

log = getLogger(__name__)


def disable_ssl_verify_warning():
    warnings.simplefilter('ignore', InsecureRequestWarning)


def preload_openssl():
    """Because our openssl library lives in Librar/bin, and because that may not be on PATH
    if conda.exe in Scripts is called directly, try this preload to avoid user issues."""
    libbin_path = os.path.join(sys.prefix, 'Library', 'bin')
    libssl_dllname = 'libssl'
    libcrypto_dllname = 'libcrypto'
    libssl_version = '-1_1'
    libssl_arch = ''
    if sys.maxsize > 2**32:
        libssl_arch = '-x64'
    so_name = libssl_dllname + libssl_version + libssl_arch
    libssl_path2 = os.path.join(libbin_path, so_name)
    # if version 1.1 is not found, try to load 1.0
    if not exists(libssl_path2 + ".dll"):
        libssl_version = ''
        libssl_arch = ''
        libssl_dllname = 'ssleay32'
        libcrypto_dllname = 'libeay32'
        so_name = libssl_dllname
        libssl_path2 = os.path.join(libbin_path, so_name)
    libssl_path = find_library(so_name)
    if not libssl_path:
        libssl_path = libssl_path2
    # crypto library might exists ...
    so_name = libcrypto_dllname + libssl_version + libssl_arch
    libcrypto_path = find_library(so_name)
    if not libcrypto_path:
        libcrypto_path = os.path.join(sys.prefix, 'Library', 'bin', so_name)
    kernel32 = ctypes.windll.kernel32
    h_mod = kernel32.GetModuleHandleA(libcrypto_path)
    if not h_mod:
        ctypes.WinDLL(libcrypto_path)
    h_mod = kernel32.GetModuleHandleA(libssl_path)
    if not h_mod:
        ctypes.WinDLL(libssl_path)


@time_recorder("download")
def download(
        url, target_full_path, md5=None, sha256=None, size=None, progress_update_callback=None
):
    if exists(target_full_path):
        maybe_raise(BasicClobberError(target_full_path, url, context), context)
    if sys.platform == 'win32':
        preload_openssl()
    if not context.ssl_verify:
        disable_ssl_verify_warning()

    try:
        timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
        session = CondaSession()
        resp = session.get(url, stream=True, proxies=session.proxies, timeout=timeout)
        if log.isEnabledFor(DEBUG):
            log.debug(stringify(resp, content_max_len=256))
        resp.raise_for_status()

        content_length = int(resp.headers.get('Content-Length', 0))

        # prefer sha256 over md5 when both are available
        checksum_builder = checksum_type = checksum = None
        if sha256:
            checksum_builder = hashlib.new("sha256")
            checksum_type = "sha256"
            checksum = sha256
        elif md5:
            checksum_builder = hashlib.new("md5") if md5 else None
            checksum_type = "md5"
            checksum = md5

        size_builder = 0
        try:
            with open(target_full_path, 'wb') as fh:
                streamed_bytes = 0
                for chunk in resp.iter_content(2 ** 14):
                    # chunk could be the decompressed form of the real data
                    # but we want the exact number of bytes read till now
                    streamed_bytes = resp.raw.tell()
                    try:
                        fh.write(chunk)
                    except IOError as e:
                        message = "Failed to write to %(target_path)s\n  errno: %(errno)d"
                        # TODO: make this CondaIOError
                        raise CondaError(message, target_path=target_full_path, errno=e.errno)

                    checksum_builder and checksum_builder.update(chunk)
                    size_builder += len(chunk)

                    if content_length and 0 <= streamed_bytes <= content_length:
                        if progress_update_callback:
                            progress_update_callback(streamed_bytes / content_length)

            if content_length and streamed_bytes != content_length:
                # TODO: needs to be a more-specific error type
                message = dals("""
                Downloaded bytes did not match Content-Length
                  url: %(url)s
                  target_path: %(target_path)s
                  Content-Length: %(content_length)d
                  downloaded bytes: %(downloaded_bytes)d
                """)
                raise CondaError(message, url=url, target_path=target_full_path,
                                 content_length=content_length,
                                 downloaded_bytes=streamed_bytes)

        except (IOError, OSError) as e:
            if e.errno == 104:
                # Connection reset by peer
                log.debug("%s, trying again" % e)
            raise

        if checksum:
            actual_checksum = checksum_builder.hexdigest()
            if actual_checksum != checksum:
                log.debug("%s mismatch for download: %s (%s != %s)",
                          checksum_type, url, actual_checksum, checksum)
                raise ChecksumMismatchError(
                    url, target_full_path, checksum_type, checksum, actual_checksum
                )
        if size is not None:
            actual_size = size_builder
            if actual_size != size:
                log.debug("size mismatch for download: %s (%s != %s)", url, actual_size, size)
                raise ChecksumMismatchError(url, target_full_path, "size", size, actual_size)

    except RequestsProxyError:
        raise ProxyError()  # see #3962

    except InvalidSchema as e:
        if 'SOCKS' in text_type(e):
            message = dals("""
                Requests has identified that your current working environment is configured
                to use a SOCKS proxy, but pysocks is not installed.  To proceed, remove your
                proxy configuration, run `conda install pysocks`, and then you can re-enable
                your proxy configuration.
                """)
            raise CondaDependencyError(message)
        else:
            raise

    except (ConnectionError, HTTPError, SSLError) as e:
        help_message = dals("""
        An HTTP error occurred when trying to retrieve this URL.
        HTTP errors are often intermittent, and a simple retry will get you on your way.
        """)
        raise CondaHTTPError(help_message,
                             url,
                             getattr(e.response, 'status_code', None),
                             getattr(e.response, 'reason', None),
                             getattr(e.response, 'elapsed', None),
                             e.response,
                             caused_by=e)


class TmpDownload(object):
    """
    Context manager to handle downloads to a tempfile
    """
    def __init__(self, url, verbose=True):
        self.url = url
        self.verbose = verbose

    def __enter__(self):
        if '://' not in self.url:
            # if we provide the file itself, no tmp dir is created
            self.tmp_dir = None
            return self.url
        else:
            self.tmp_dir = tempfile.mkdtemp()
            dst = join(self.tmp_dir, basename(self.url))
            download(self.url, dst)
            return dst

    def __exit__(self, exc_type, exc_value, traceback):
        if self.tmp_dir:
            rm_rf(self.tmp_dir)
