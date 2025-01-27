# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.
import logging
import yaml

from django.core.cache import cache as django_cache
from django.core.management.base import BaseCommand
from django.db import connection as django_connection

from awx.main.dispatch import get_task_queuename
from awx.main.dispatch.control import Control
from awx.main.dispatch.pool import AutoscalePool
from awx.main.dispatch.worker import AWXConsumerPG, TaskWorker
from awx.main.dispatch import periodic

logger = logging.getLogger('awx.main.dispatch')


class Command(BaseCommand):
    help = 'Launch the task dispatcher'

    def add_arguments(self, parser):
        parser.add_argument('--status', dest='status', action='store_true', help='print the internal state of any running dispatchers')
        parser.add_argument('--running', dest='running', action='store_true', help='print the UUIDs of any tasked managed by this dispatcher')
        parser.add_argument(
            '--reload',
            dest='reload',
            action='store_true',
            help=('cause the dispatcher to recycle all of its worker processes; running jobs will run to completion first'),
        )
        parser.add_argument(
            '--cancel',
            dest='cancel',
            help=(
                'Cancel a particular task id. Takes either a single id string, or a JSON list of multiple ids. '
                'Can take in output from the --running argument as input to cancel all tasks. '
                'Only running tasks can be canceled, queued tasks must be started before they can be canceled.'
            ),
        )

    def handle(self, *arg, **options):
        if options.get('status'):
            print(Control('dispatcher').status())
            return
        if options.get('running'):
            print(Control('dispatcher').running())
            return
        if options.get('reload'):
            return Control('dispatcher').control({'control': 'reload'})
        if options.get('cancel'):
            cancel_str = options.get('cancel')
            try:
                cancel_data = yaml.safe_load(cancel_str)
            except Exception:
                cancel_data = [cancel_str]
            if not isinstance(cancel_data, list):
                cancel_data = [cancel_str]
            print(Control('dispatcher').cancel(cancel_data))
            return

        # It's important to close these because we're _about_ to fork, and we
        # don't want the forked processes to inherit the open sockets
        # for the DB and cache connections (that way lies race conditions)
        django_connection.close()
        django_cache.close()

        # spawn a daemon thread to periodically enqueues scheduled tasks
        # (like the node heartbeat)
        periodic.run_continuously()

        consumer = None

        try:
            queues = ['tower_broadcast_all', 'tower_settings_change', get_task_queuename()]
            consumer = AWXConsumerPG('dispatcher', TaskWorker(), queues, AutoscalePool(min_workers=4))
            consumer.run()
        except KeyboardInterrupt:
            logger.debug('Terminating Task Dispatcher')
            if consumer:
                consumer.stop()
