#!/usr/bin/python3
#
# Send build performance test report emails
#
# Copyright (c) 2017, Intel Corporation.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
import argparse
import base64
import logging
import os
import pwd
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import tempfile
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# Setup logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger('oe-build-perf-report')


# Find js scaper script
SCRAPE_JS = os.path.join(os.path.dirname(__file__), '..', 'lib', 'build_perf',
                         'scrape-html-report.js')
if not os.path.isfile(SCRAPE_JS):
    log.error("Unableto find oe-build-perf-report-scrape.js")
    sys.exit(1)


class ReportError(Exception):
    """Local errors"""
    pass


def check_utils():
    """Check that all needed utils are installed in the system"""
    missing = []
    for cmd in ('phantomjs', 'optipng'):
        if not shutil.which(cmd):
            missing.append(cmd)
    if missing:
        log.error("The following tools are missing: %s", ' '.join(missing))
        sys.exit(1)


def parse_args(argv):
    """Parse command line arguments"""
    description = """Email build perf test report"""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=description)

    parser.add_argument('--debug', '-d', action='store_true',
                        help="Verbose logging")
    parser.add_argument('--quiet', '-q', action='store_true',
                        help="Only print errors")
    parser.add_argument('--to', action='append',
                        help="Recipients of the email")
    parser.add_argument('--cc', action='append',
                        help="Carbon copy recipients of the email")
    parser.add_argument('--bcc', action='append',
                        help="Blind carbon copy recipients of the email")
    parser.add_argument('--subject', default="Yocto build perf test report",
                        help="Email subject")
    parser.add_argument('--outdir', '-o',
                        help="Store files in OUTDIR. Can be used to preserve "
                             "the email parts")
    parser.add_argument('--text',
                        help="Plain text message")
    parser.add_argument('--html',
                        help="HTML peport generated by oe-build-perf-report")
    parser.add_argument('--phantomjs-args', action='append',
                        help="Extra command line arguments passed to PhantomJS")

    args = parser.parse_args(argv)

    if not args.html and not args.text:
        parser.error("Please specify --html and/or --text")

    return args


def decode_png(infile, outfile):
    """Parse/decode/optimize png data from a html element"""
    with open(infile) as f:
        raw_data = f.read()

    # Grab raw base64 data
    b64_data = re.sub('^.*href="data:image/png;base64,', '', raw_data, 1)
    b64_data = re.sub('">.+$', '', b64_data, 1)

    # Replace file with proper decoded png
    with open(outfile, 'wb') as f:
        f.write(base64.b64decode(b64_data))

    subprocess.check_output(['optipng', outfile], stderr=subprocess.STDOUT)


def mangle_html_report(infile, outfile, pngs):
    """Mangle html file into a email compatible format"""
    paste = True
    png_dir = os.path.dirname(outfile)
    with open(infile) as f_in:
        with open(outfile, 'w') as f_out:
            for line in f_in.readlines():
                stripped = line.strip()
                # Strip out scripts
                if stripped == '<!--START-OF-SCRIPTS-->':
                    paste = False
                elif stripped == '<!--END-OF-SCRIPTS-->':
                    paste = True
                elif paste:
                    if re.match('^.+href="data:image/png;base64', stripped):
                        # Strip out encoded pngs (as they're huge in size)
                        continue
                    elif 'www.gstatic.com' in stripped:
                        # HACK: drop references to external static pages
                        continue

                    # Replace charts with <img> elements
                    match = re.match('<div id="(?P<id>\w+)"', stripped)
                    if match and match.group('id') in pngs:
                        f_out.write('<img src="cid:{}"\n'.format(match.group('id')))
                    else:
                        f_out.write(line)


def scrape_html_report(report, outdir, phantomjs_extra_args=None):
    """Scrape html report into a format sendable by email"""
    tmpdir = tempfile.mkdtemp(dir='.')
    log.debug("Using tmpdir %s for phantomjs output", tmpdir)

    if not os.path.isdir(outdir):
        os.mkdir(outdir)
    if os.path.splitext(report)[1] not in ('.html', '.htm'):
        raise ReportError("Invalid file extension for report, needs to be "
                          "'.html' or '.htm'")

    try:
        log.info("Scraping HTML report with PhangomJS")
        extra_args = phantomjs_extra_args if phantomjs_extra_args else []
        subprocess.check_output(['phantomjs', '--debug=true'] + extra_args +
                                [SCRAPE_JS, report, tmpdir],
                                stderr=subprocess.STDOUT)

        pngs = []
        images = []
        for fname in os.listdir(tmpdir):
            base, ext = os.path.splitext(fname)
            if ext == '.png':
                log.debug("Decoding %s", fname)
                decode_png(os.path.join(tmpdir, fname),
                           os.path.join(outdir, fname))
                pngs.append(base)
                images.append(fname)
            elif ext in ('.html', '.htm'):
                report_file = fname
            else:
                log.warning("Unknown file extension: '%s'", ext)
                #shutil.move(os.path.join(tmpdir, fname), outdir)

        log.debug("Mangling html report file %s", report_file)
        mangle_html_report(os.path.join(tmpdir, report_file),
                           os.path.join(outdir, report_file), pngs)
        return (os.path.join(outdir, report_file),
                [os.path.join(outdir, i) for i in images])
    finally:
        shutil.rmtree(tmpdir)

def send_email(text_fn, html_fn, image_fns, subject, recipients, copy=[],
               blind_copy=[]):
    """Send email"""
    # Generate email message
    text_msg = html_msg = None
    if text_fn:
        with open(text_fn) as f:
            text_msg = MIMEText("Yocto build performance test report.\n" +
                                f.read(), 'plain')
    if html_fn:
        html_msg = msg = MIMEMultipart('related')
        with open(html_fn) as f:
            html_msg.attach(MIMEText(f.read(), 'html'))
        for img_fn in image_fns:
            # Expect that content id is same as the filename
            cid = os.path.splitext(os.path.basename(img_fn))[0]
            with open(img_fn, 'rb') as f:
                image_msg = MIMEImage(f.read())
            image_msg['Content-ID'] = '<{}>'.format(cid)
            html_msg.attach(image_msg)

    if text_msg and html_msg:
        msg = MIMEMultipart('alternative')
        msg.attach(text_msg)
        msg.attach(html_msg)
    elif text_msg:
        msg = text_msg
    elif html_msg:
        msg = html_msg
    else:
        raise ReportError("Neither plain text nor html body specified")

    pw_data = pwd.getpwuid(os.getuid())
    full_name = pw_data.pw_gecos.split(',')[0]
    email = os.environ.get('EMAIL',
                           '{}@{}'.format(pw_data.pw_name, socket.getfqdn()))
    msg['From'] = "{} <{}>".format(full_name, email)
    msg['To'] = ', '.join(recipients)
    if copy:
        msg['Cc'] = ', '.join(copy)
    if blind_copy:
        msg['Bcc'] = ', '.join(blind_copy)
    msg['Subject'] = subject

    # Send email
    with smtplib.SMTP('localhost') as smtp:
        smtp.send_message(msg)


def main(argv=None):
    """Script entry point"""
    args = parse_args(argv)
    if args.quiet:
        log.setLevel(logging.ERROR)
    if args.debug:
        log.setLevel(logging.DEBUG)

    check_utils()

    if args.outdir:
        outdir = args.outdir
        if not os.path.exists(outdir):
            os.mkdir(outdir)
    else:
        outdir = tempfile.mkdtemp(dir='.')

    try:
        log.debug("Storing email parts in %s", outdir)
        html_report = images = None
        if args.html:
            html_report, images = scrape_html_report(args.html, outdir,
                                                     args.phantomjs_args)

        if args.to:
            log.info("Sending email to %s", ', '.join(args.to))
            if args.cc:
                log.info("Copying to %s", ', '.join(args.cc))
            if args.bcc:
                log.info("Blind copying to %s", ', '.join(args.bcc))
            send_email(args.text, html_report, images, args.subject,
                       args.to, args.cc, args.bcc)
    except subprocess.CalledProcessError as err:
        log.error("%s, with output:\n%s", str(err), err.output.decode())
        return 1
    except ReportError as err:
        log.error(err)
        return 1
    finally:
        if not args.outdir:
            log.debug("Wiping %s", outdir)
            shutil.rmtree(outdir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
