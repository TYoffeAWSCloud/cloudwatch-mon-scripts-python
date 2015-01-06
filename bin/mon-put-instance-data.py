#!/usr/bin/python

# Copyright 2015 Oliver Siegmar
#
# Based on Perl-Version of CloudWatch Monitoring Scripts for Linux -
# Copyright 2013 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__requires__ = 'boto>=2.33.0'

import argparse
import boto
import boto.ec2.autoscale
import boto.ec2.cloudwatch
import boto.utils
import datetime
import hashlib
import os
import pickle
import pkg_resources
import random
import re
import sys
import syslog
import time

CLIENT_NAME = 'CloudWatch-PutInstanceData'
VERSION = '2.0.0-beta'
META_DATA_CACHE_DIR = os.environ.get('AWS_EC2CW_META_DATA', '/var/tmp/aws-mon')
META_DATA_CACHE_TTL = os.environ.get('AWS_EC2CW_META_DATA_TTL', 21600)

SIZE_UNITS_CFG = {
    'bytes': {'name': 'Bytes', 'div': 1},
    'kilobytes': {'name': 'Kilobytes', 'div': 1024},
    'megabytes': {'name': 'Megabytes', 'div': 1048576},
    'gigabytes': {'name': 'Gigabytes', 'div': 1073741824}
}


class MemData:
    def __init__(self, mem_used_incl_cache_buff):
        self.mem_used_incl_cache_buff = mem_used_incl_cache_buff
        mem_info = self.__gather_mem_info()
        self.mem_total = mem_info['MemTotal']
        self.mem_free = mem_info['MemFree']
        self.mem_cached = mem_info['Cached']
        self.mem_buffers = mem_info['Buffers']
        self.swap_total = mem_info['SwapTotal']
        self.swap_free = mem_info['SwapFree']

    @staticmethod
    def __gather_mem_info():
        meminfo = {}
        pattern = re.compile(r'^(?P<key>\S*):\s*(?P<value>\d*)\s*kB')
        with open('/proc/meminfo') as f:
            for line in f:
                match = pattern.match(line)
                if match:
                    key, value = match.groups(['key', 'value'])
                    meminfo[key] = int(value) * 1024
        return meminfo

    def mem_util(self):
        return 100.0 * self.mem_used() / self.mem_total

    def mem_used(self):
        return self.mem_total - self.mem_avail()

    def mem_avail(self):
        mem_avail = self.mem_free
        if not self.mem_used_incl_cache_buff:
            mem_avail += self.mem_cached + self.mem_buffers

        return mem_avail

    def swap_util(self):
        if self.swap_total == 0:
            return 0

        return 100.0 * self.swap_used() / self.swap_total

    def swap_used(self):
        return self.swap_total - self.swap_free


class Disk:
    def __init__(self, mount, file_system, total, used, avail):
        self.mount = mount
        self.file_system = file_system
        self.used = used
        self.avail = avail
        self.util = 100.0 * used / total if total > 0 else 0


class Metrics:
    def __init__(self, instance_id, instance_type, image_id, aggregated,
                 auto_scaling_group):
        self.names = []
        self.units = []
        self.values = []
        self.dimensions = []
        self.instance_id = instance_id
        self.instance_type = instance_type
        self.image_id = image_id
        self.aggregated = aggregated
        self.auto_scaling_group_name = auto_scaling_group

    def add_metric(self, name, unit, value, mount=None, file_system=None):
        common_dims = {}
        if mount:
            common_dims['MountPath'] = mount
        if file_system:
            common_dims['Filesystem'] = file_system

        dims = []

        if self.aggregated != 'only':
            dims.append({'InstanceId': self.instance_id})

        if self.auto_scaling_group_name:
            dims.append({'AutoScalingGroupName': self.auto_scaling_group_name})

        if self.aggregated:
            dims.append({'InstanceType': self.instance_type})
            dims.append({'ImageId': self.image_id})
            dims.append({})

        self.__add_metric_dimensions(name, unit, value, common_dims, dims)

    def __add_metric_dimensions(self, name, unit, value, common_dims, dims):
        for dim in dims:
            self.names.append(name)
            self.units.append(unit)
            self.values.append(value)
            self.dimensions.append(dict(common_dims.items() + dim.items()))

    def __str__(self):
        ret = ''
        for i in range(0, len(self.names)):
            ret += '{0}: {1} {2} ({3})\n'.format(self.names[i],
                                                 self.values[i],
                                                 self.units[i],
                                                 self.dimensions[i])
        return ret


class FileCache:
    def __init__(self, fnc):
        self.fnc = fnc
        if not os.path.exists(META_DATA_CACHE_DIR):
            os.makedirs(META_DATA_CACHE_DIR)

    def __call__(self, *args, **kwargs):
        sig = str(self.fnc.__name__) + ':' + str(args) + ':' + str(kwargs)
        filename = os.path.join(META_DATA_CACHE_DIR, '{0}-{1}.bin'
                                .format(CLIENT_NAME,
                                        hashlib.md5(sig).hexdigest()))

        if os.path.exists(filename):
            mtime = os.path.getmtime(filename)
            now = time.time()
            if mtime + META_DATA_CACHE_TTL > now:
                with open(filename, 'rb') as f:
                    return pickle.load(f)

        tmp = self.fnc(*args, **kwargs)
        with open(filename, 'wb') as f:
            os.chmod(filename, 0600)
            pickle.dump(tmp, f)

        return tmp


def to_lower(s):
    return s.lower()


def config_parser():
    size_units = ['bytes', 'kilobytes', 'megabytes', 'gigabytes']
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
  Collects memory, swap, and disk space utilization on an Amazon EC2 instance
  and sends this data as custom metrics to Amazon CloudWatch.''', epilog='''
Supported UNITS are bytes, kilobytes, megabytes, and gigabytes.

Examples

 To perform a simple test run without posting data to Amazon CloudWatch

  ./mon-put-instance-data.py --mem-util --verify --verbose

 To set a five-minute cron schedule to report memory and disk space utilization
 to CloudWatch

  */5 * * * * ~/aws-scripts-mon/mon-put-instance-data.py --mem-util --disk-space-util --disk-path=/ --from-cron

For more information on how to use this utility, see project home on GitHub:
https://github.com/osiegmar/cloudwatch-mon-scripts-python
    ''')

    memory_group = parser.add_argument_group('memory metrics')
    memory_group.add_argument('--mem-util',
                              action='store_true',
                              help='Reports memory utilization in percentages.')
    memory_group.add_argument('--mem-used',
                              action='store_true',
                              help='Reports memory used in megabytes.')
    memory_group.add_argument('--mem-avail',
                              action='store_true',
                              help='Reports available memory in megabytes.')
    memory_group.add_argument('--swap-util',
                              action='store_true',
                              help='Reports swap utilization in percentages.')
    memory_group.add_argument('--swap-used',
                              action='store_true',
                              help='Reports allocated swap space in megabytes.')
    memory_group.add_argument('--mem-used-incl-cache-buff',
                              action='store_true',
                              help='Count memory that is cached and in buffers as used.')
    memory_group.add_argument('--memory-units',
                              metavar='UNITS',
                              default='megabytes',
                              type=to_lower,
                              choices=size_units,
                              help='Specifies units for memory metrics.')

    disk_group = parser.add_argument_group('disk metrics')
    disk_group.add_argument('--disk-path',
                            metavar='PATH',
                            action='append',
                            help='Selects the disk by the path on which to report.')
    disk_group.add_argument('--disk-space-util',
                            action='store_true',
                            help='Reports disk space utilization in percentages.')
    disk_group.add_argument('--disk-space-used',
                            action='store_true',
                            help='Reports allocated disk space in gigabytes.')
    disk_group.add_argument('--disk-space-avail',
                            action='store_true',
                            help='Reports available disk space in gigabytes.')
    disk_group.add_argument('--disk-space-units',
                            metavar='UNITS',
                            default='gigabytes',
                            type=to_lower,
                            choices=size_units,
                            help='Specifies units for disk space metrics.')

    exclusive_group = parser.add_mutually_exclusive_group()
    exclusive_group.add_argument('--from-cron',
                                 action='store_true',
                                 help='Specifies that this script is running from cron.')
    exclusive_group.add_argument('--verbose',
                                 action='store_true',
                                 help='Displays details of what the script is doing.')

    parser.add_argument('--aggregated',
                        type=to_lower,
                        choices=['additional', 'only'],
                        const='additional',
                        nargs='?',
                        help='Adds aggregated metrics for instance type, AMI id, and overall.')
    parser.add_argument('--auto-scaling',
                        type=to_lower,
                        choices=['additional', 'only'],
                        const='additional',
                        nargs='?',
                        help='Adds aggregated metrics for Auto Scaling group.')
    parser.add_argument('--verify',
                        action='store_true',
                        help='Checks configuration and prepares a remote call.')
    parser.add_argument('--version',
                        action='store_true',
                        help='Displays the version number and exits.')

    return parser


def add_memory_metrics(args, metrics):
    mem = MemData(args.mem_used_incl_cache_buff)

    mem_unit_name = SIZE_UNITS_CFG[args.memory_units]['name']
    mem_unit_div = float(SIZE_UNITS_CFG[args.memory_units]['div'])
    if args.mem_util:
        metrics.add_metric('MemoryUtilization', 'Percent', mem.mem_util())
    if args.mem_used:
        metrics.add_metric('MemoryUsed', mem_unit_name,
                           mem.mem_used() / mem_unit_div)
    if args.mem_avail:
        metrics.add_metric('MemoryAvailable', mem_unit_name,
                           mem.mem_avail() / mem_unit_div)
    if args.swap_util:
        metrics.add_metric('SwapUtilization', 'Percent', mem.swap_util())
    if args.swap_used:
        metrics.add_metric('SwapUsed', mem_unit_name,
                           mem.swap_used() / mem_unit_div)


def get_disk_info(paths):
    df_out = [s.split() for s in
              os.popen('/bin/df -k -l -P ' +
                       ' '.join(paths)).read().splitlines()]
    disks = []
    for line in df_out[1:]:
        mount = line[5]
        file_system = line[0]
        total = int(line[1]) * 1024
        used = int(line[2]) * 1024
        avail = int(line[3]) * 1024
        disks.append(Disk(mount, file_system, total, used, avail))
    return disks


def add_disk_metrics(args, metrics):
    disk_unit_name = SIZE_UNITS_CFG[args.disk_space_units]['name']
    disk_unit_div = float(SIZE_UNITS_CFG[args.disk_space_units]['div'])
    disks = get_disk_info(args.disk_path)
    for disk in disks:
        if args.disk_space_util:
            metrics.add_metric('DiskSpaceUtilization', 'Percent',
                               disk.util, disk.mount, disk.file_system)
        if args.disk_space_used:
            metrics.add_metric('DiskSpaceUsed', disk_unit_name,
                               disk.used / disk_unit_div,
                               disk.mount, disk.file_system)
        if args.disk_space_avail:
            metrics.add_metric('DiskSpaceAvailable', disk_unit_name,
                               disk.avail / disk_unit_div,
                               disk.mount, disk.file_system)


def log_error(message, use_syslog):
    if use_syslog:
        syslog.syslog(syslog.LOG_ERR, message)
    else:
        print >> sys.stderr, 'ERROR: ' + message


def send_metrics(region, metrics, verbose):
    boto_debug = 2 if verbose else 0

    # TODO add timeout
    conn = boto.ec2.cloudwatch.connect_to_region(region, debug=boto_debug)
    if not conn:
        raise IOError('Could not establish connection to CloudWatch service')

    response = conn.put_metric_data('System/Linux', metrics.names,
                                    value=metrics.values,
                                    timestamp=datetime.datetime.utcnow(),
                                    unit=metrics.units,
                                    dimensions=metrics.dimensions)

    if not response:
        raise ValueError('Could not send data to CloudWatch - '
                         'use --verbose for more information')


@FileCache
def get_autoscaling_group(region, instance_id, verbose):
    boto_debug = 2 if verbose else 0

    # TODO add timeout
    conn = boto.ec2.autoscale.connect_to_region(region, debug=boto_debug)

    if not conn:
        raise IOError('Could not establish connection to CloudWatch service')

    autoscaling_instances = conn.get_all_autoscaling_instances([instance_id])

    if not autoscaling_instances:
        raise ValueError('Could not find auto-scaling information')

    return autoscaling_instances[0].group_name


@FileCache
def get_metadata():
    metadata = boto.utils.get_instance_metadata(timeout=1, num_retries=2)
    if not metadata:
        raise ValueError('Cannot obtain EC2 metadata.')
    return metadata


def validate_args(args):
    report_mem_data = args.mem_util or args.mem_used or args.mem_avail or \
        args.swap_util or args.swap_used
    report_disk_data = args.disk_path is not None

    if report_disk_data:
        if not args.disk_space_util and not args.disk_space_used and \
                not args.disk_space_avail:
            raise ValueError('Disk path is provided but metrics to report '
                             'disk space are not specified.')

        for path in args.disk_path:
            if not os.path.isdir(path):
                raise ValueError('Disk file path ' + path +
                                 ' does not exist or cannot be accessed.')
    elif args.disk_space_util or args.disk_space_used or \
            args.disk_space_avail:
        raise ValueError('Metrics to report disk space are provided but '
                         'disk path is not specified.')

    if not report_mem_data and not report_disk_data:
        raise ValueError('No metrics specified for collection and '
                         'submission to CloudWatch.')

    return report_disk_data, report_mem_data


def main():
    parser = config_parser()

    # exit with help, because no args specified
    if len(sys.argv) == 1:
        parser.print_help()
        return 1

    args = parser.parse_args()

    if args.version:
        print CLIENT_NAME + ' version ' + VERSION
        return 0

    try:
        report_disk_data, report_mem_data = validate_args(args)

        # avoid a storm of calls at the beginning of a minute
        if args.from_cron:
            time.sleep(random.randint(0, 19))

        if args.verbose:
            print 'Working in verbose mode'
            print 'Boto-Version: ' + boto.__version__

        metadata = get_metadata()

        if args.verbose:
            print 'Instance metadata: ' + str(metadata)

        region = metadata['placement']['availability-zone'][:-1]
        instance_id = metadata['instance-id']
        autoscaling_group = None
        if args.auto_scaling:
            autoscaling_group = get_autoscaling_group(region, instance_id,
                                                      args.verbose)

            if args.verbose:
                print 'Autoscaling group: ' + autoscaling_group

        metrics = Metrics(instance_id,
                          metadata['instance-type'],
                          metadata['ami-id'],
                          args.aggregated,
                          autoscaling_group)

        if report_mem_data:
            add_memory_metrics(args, metrics)

        if report_disk_data:
            add_disk_metrics(args, metrics)

        if args.verbose:
            print 'Request:\n' + str(metrics)

        if args.verify:
            if not args.from_cron:
                print 'Verification completed successfully. ' \
                      'No actual metrics sent to CloudWatch.'
        else:
            send_metrics(region, metrics, args.verbose)
            if not args.from_cron:
                print 'Successfully reported metrics to CloudWatch.'
    except Exception as e:
        log_error(e.message, args.from_cron)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
