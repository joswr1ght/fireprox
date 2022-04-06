#!/usr/bin/env python3
from multiprocessing import Pool
from pathlib import Path
import shutil
import tldextract
import os
import sys
import datetime
import tzlocal
import argparse
import json
import configparser
from typing import Tuple, Callable
import string
import random
import docker
import time


class FireProx(object):
    def __init__(self, arguments: argparse.Namespace, help_text: str):
        self.profile_name = arguments.profile_name
        self.access_key = arguments.access_key
        self.secret_access_key = arguments.secret_access_key
        self.session_token = arguments.session_token
        self.region = arguments.region
        self.command = arguments.command
        self.api_id = arguments.api_id
        self.url = arguments.url
        self.api_list = []
        self.client = None
        self.help = help_text

        if (self.region == None):
            self.region = 'us-east-1'

        if self.access_key and self.secret_access_key:
            if not self.region:
                self.error('Please provide a region with AWS credentials')

        if not self.load_creds():
            self.error('Unable to load AWS credentials')

        if not self.command:
            self.error('Please provide a valid command')


    def __str__(self):
        return 'FireProx()'


    def _try_instance_profile(self) -> bool:
        """Try instance profile credentials

        :return:
        """
        # This is all hack to simulate AWS access without actual AWS; remove
        # calls to boto3.*
        return True


    def load_creds(self) -> bool:
        """Load credentials from AWS config and credentials files if present.

        :return:
        """
        # If no access_key, secret_key, or profile name provided, try instance credentials
        if not any([self.access_key, self.secret_access_key, self.profile_name]):
            return self._try_instance_profile()
        # Read in AWS config/credentials files if they exist
        credentials = configparser.ConfigParser()
        credentials.read(os.path.expanduser('~/.aws/credentials'))
        config = configparser.ConfigParser()
        config.read(os.path.expanduser('~/.aws/config'))
        # If profile in files, try it, but flow through if it does not work
        config_profile_section = f'profile {self.profile_name}'
        if self.profile_name in credentials:
            if config_profile_section not in config:
                print(f'Please create a section for {self.profile_name} in your ~/.aws/config file')
                return False
            self.region = config[config_profile_section].get('region', 'us-east-1')
            try:
#                self.client = boto3.session.Session(profile_name=self.profile_name,
#                        region_name=self.region).client('apigateway')
#                self.client.get_account()
                return True
            except:
                pass
        # Maybe had profile, maybe didn't
        if self.access_key and self.secret_access_key:
            try:
                if self.profile_name:
                    if config_profile_section not in config:
                        config.add_section(config_profile_section)
                    config[config_profile_section]['region'] = self.region
                    with open(os.path.expanduser('~/.aws/config'), 'w') as file:
                        config.write(file)
                    if self.profile_name not in credentials:
                        credentials.add_section(self.profile_name)
                    credentials[self.profile_name]['aws_access_key_id'] = self.access_key
                    credentials[self.profile_name]['aws_secret_access_key'] = self.secret_access_key
                    if self.session_token:
                        credentials[self.profile_name]['aws_session_token'] = self.session_token
                    else:
                        credentials.remove_option(self.profile_name, 'aws_session_token')
                    with open(os.path.expanduser('~/.aws/credentials'), 'w') as file:
                        credentials.write(file)
                return True
            except:
                return False
        else:
            return False


    def error(self, error):
        print(self.help)
        sys.exit(error)


    def _generate_app_id(self):
        return ''.join(random.choice(string.ascii_lowercase + string.digits) for i in range(10))


    def _get_container_ip(self, container, networkname=None):
        '''
        Get the IP address of the specified docker.models.containers.Container object.
        Use the specified networkname, otherwise return the first IP address.
        Return None for network name not found or no IP address.
        '''
        network=container.attrs['NetworkSettings']['Networks']
        if (networkname != None):
            ipaddress = network[networkname]['IPAddress']
            if (ipaddress):
                return ipaddress
        else:
            # Return the IP address for the first network found
            ip = None
            for netkey in network:
                ipaddress = network[netkey]['IPAddress']
                if (ipaddress):
                    return ipaddress

        return None


    def _add_hosts(self, container, networkname=None):
        '''
        Add the docker.models.containers.Container hostname and IP address to /etc/hosts.
        '''
        hostname = container.attrs['Config']['Hostname']
        ip = self._get_container_ip(container, networkname)
        with open('/etc/hosts', 'a') as hostsf:
            hostsf.write(f'{ip} {hostname}\n')


    def _remove_hosts(self, container):
        '''
        Remove the dockers.models.containers.Container hostname from /etc/hosts.
        '''
        hostname = container.attrs['Config']['Hostname']
        hosts = []
        with open('/etc/hosts', 'r') as hostsf:
            for line in hostsf.readlines():
                if (hostname not in line):
                    hosts.append(line)
        with open('/etc/hosts', 'w') as hostsf:
            hostsf.write(''.join(hosts))


    def create_api(self, url):
        if not url:
            self.error('Please provide a valid URL end-point')

        print(f'Creating => {url}...')

        # Create the fake API endpoint by launching a Docker container
        client = docker.from_env()
        app_id = self._generate_app_id()
        region = self.region
        # Unlike the real fireprox, we only take a hostname argument
        target=url.split('/')[2]
        self._containername = f'{app_id}.execute-api.{region}.amazonaws.com'

        container = client.containers.run('execute-api.amazonaws.com',
                detach=True,  # -d
                privileged=True,  # --privileged
                network='sec504cloudsim-far',  # --net
                remove=True,  # --rm
                init=True,  # --init
                environment={'JWAPIGW_TARGET':target},  # -e
                hostname=self._containername,  # -h
                stdin_open=True,  # -i
                tty=True,  # -t
                name=self._containername)  # --name
        self._containerid = container.id

        # Give container time to start up
        while self._get_container_ip(container, 'sec504cloudsim-far') == None:
            time.sleep(1)
            container.reload()

        # We need this hack for Docker routing to work back to the host system
        container.exec_run('ip route del default')
        container.exec_run('ip route add default via 10.200.0.2')

        # Save hostname to local /etc/hosts file
        self._add_hosts(container, 'sec504cloudsim-far')

        self._print_list([container,])

    def _print_list(self, containers):
        '''
        Accept a list of Containers, and print a line of status for each.
        Displays only containers matching the FireProx naming convention.
        Intended to be called using client.containers.list() or with a single
        Container object.
        '''
        for container in containers:
            containername = container.name
            if ('execute-api' not in containername or 'amazonaws.com' not in containername):
                continue
            app_id = containername.split('.')[0]
            url = 'http://' + container.attrs['Config']['Env'][0].split('=')[1] # Whole lot of assumptions here
            # This is what we get from Docker --  2022-04-06T12:47:31.747842357Z
            # This is what we want to match for FireProx -- [2022-04-03 07:34:41-04:00]
            timestamp = container.attrs['Created']
            day, time_ = timestamp.split('T')
            time_ = time_.split('.')[0] + '-00:00'
            domain = tldextract.extract(url).domain

            print(f'[{day} {time_}] ({app_id}) fireprox_{domain} => http://{containername}/ ({url})')


    def update_api(self, api_id, url):
        self.error('Not implemented (for this lab exercise)')


    def delete_api(self, api_id):
        if not api_id:
            self.error('Please provide a valid API ID')
        client = docker.from_env()
        for container in client.containers.list():
            if (api_id in container.name):
                # Stop this container
                container.stop()
                # Remove /etc/hosts entry
                self._remove_hosts(container)
                return True
        return False


    def list_api(self, deleted_api_id=None):
        client = docker.from_env()
        self._print_list(client.containers.list())


    def store_api(self, api_id, name, created_dt, version_dt, url,
                  resource_id, proxy_url):
        print(
            f'[{created_dt}] ({api_id}) {name} => {proxy_url} ({url})'
        )


    def create_deployment(self, api_id):
        if not api_id:
            self.error('Please provide a valid API ID')

        response = self.client.create_deployment(
            restApiId=api_id,
            stageName='fireprox',
            stageDescription='FireProx Prod',
            description='FireProx Production Deployment'
        )
        resource_id = response['id']
        return (resource_id,
                f'https://{api_id}.execute-api.{self.region}.amazonaws.com/fireprox/')


    def get_resource(self, api_id):
        if not api_id:
            self.error('Please provide a valid API ID')
        response = self.client.get_resources(restApiId=api_id)
        items = response['items']
        for item in items:
            item_id = item['id']
            item_path = item['path']
            if item_path == '/{proxy+}':
                return item_id
        return None


    def get_integration(self, api_id):
        if not api_id:
            self.error('Please provide a valid API ID')
        resource_id = self.get_resource(api_id)
        response = self.client.get_integration(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod='ANY'
        )
        return response['uri']


def parse_arguments() -> Tuple[argparse.Namespace, str]:
    """Parse command line arguments and return namespace

    :return: Namespace for arguments and help text as a tuple
    """
    parser = argparse.ArgumentParser(description='FireProx API Gateway Manager')
    parser.add_argument('--profile_name',
                        help='AWS Profile Name to store/retrieve credentials', type=str, default=None)
    parser.add_argument('--access_key',
                        help='AWS Access Key', type=str, default=None)
    parser.add_argument('--secret_access_key',
                        help='AWS Secret Access Key', type=str, default=None)
    parser.add_argument('--session_token',
                        help='AWS Session Token', type=str, default=None)
    parser.add_argument('--region',
                        help='AWS Region', type=str, default=None)
    parser.add_argument('--command',
                        help='Commands: list, create, delete, update', type=str, default=None)
    parser.add_argument('--api_id',
                        help='API ID', type=str, required=False)
    parser.add_argument('--url',
                        help='URL end-point', type=str, required=False)
    return parser.parse_args(), parser.format_help()


def main():
    """Run the main program

    :return:
    """

    print('''FireProx - Modified for lab use. Do not use this version outside of a lab. To
use FireProx in production on this system, run /opt/fireprox/fire.py instead.
''')

    if os.geteuid() != 0:
        sys.stderr.write('This lab version of FireProx requires root access. Please run with sudo.\n')
        sys.exit(1)

    args, help_text = parse_arguments()
    fp = FireProx(args, help_text)
    if args.command == 'list':
        print(f'Listing API\'s...')
        result = fp.list_api()

    elif args.command == 'create':
        result = fp.create_api(fp.url)

    elif args.command == 'delete':
        result = fp.delete_api(fp.api_id)
        success = 'Success!' if result else 'Failed!'
        print(f'Deleting {fp.api_id} => {success}')

    elif args.command == 'update':
        print(f'Updating {fp.api_id} => {fp.url}...')
        result = fp.update_api(fp.api_id, fp.url)
        success = 'Success!' if result else 'Failed!'
        print(f'API Update Complete: {success}')


if __name__ == '__main__':
    main()
