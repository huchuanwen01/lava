# Copyright (C) 2014 Linaro Limited
#
# Author: Neil Williams <neil.williams@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

# List just the subclasses supported for this base strategy
# imported by the parser to populate the list of subclasses.

from lava_dispatcher.action import (
    ConfigurationError,
    Pipeline,
)
from lava_dispatcher.logical import Boot
from lava_dispatcher.actions.boot import (
    BootAction,
    AutoLoginAction,
    BootloaderCommandOverlay,
    BootloaderCommandsAction,
    BootloaderSecondaryMedia,
    OverlayUnpack,
    BootloaderInterruptAction
)
from lava_dispatcher.actions.boot.environment import ExportDeviceEnvironment
from lava_dispatcher.shell import ExpectShellSession
from lava_dispatcher.connections.lxc import ConnectLxc
from lava_dispatcher.connections.serial import ConnectDevice
from lava_dispatcher.power import ResetDevice
from lava_dispatcher.utils.strings import map_kernel_uboot
from lava_dispatcher.utils.storage import FlashUBootUMSAction
from lava_dispatcher.utils.udev import WaitDevicePathAction


class UBoot(Boot):
    """
    The UBoot method prepares the command to run on the dispatcher but this
    command needs to start a new connection and then interrupt u-boot.
    An expect shell session can then be handed over to the UBootAction.
    self.run_command is a blocking call, so Boot needs to use
    a direct spawn call via ShellCommand (which wraps pexpect.spawn) then
    hand this pexpect wrapper to subsequent actions as a shell connection.
    """

    compatibility = 1

    def __init__(self, parent, parameters):
        super(UBoot, self).__init__(parent)
        self.action = UBootAction()
        self.action.section = self.action_type
        self.action.job = self.job
        parent.add_action(self.action, parameters)

    @classmethod
    def accepts(cls, device, parameters):
        if parameters['method'] != 'u-boot':
            return False, '"method" was not "u-boot"'
        if 'commands' not in parameters:
            raise ConfigurationError("commands not specified in boot parameters")
        if 'u-boot' in device['actions']['boot']['methods']:
            return True, 'accepted'
        else:
            return False, '"u-boot" was not in the device configuration boot methods'


class UBootAction(BootAction):
    """
    Wraps the Retry Action to allow for actions which precede
    the reset, e.g. Connect.
    """

    name = "uboot-action"
    description = "interactive uboot action"
    summary = "pass uboot commands"

    def validate(self):
        super(UBootAction, self).validate()
        if 'type' in self.parameters:
            self.logger.warning("Specifying a type in the boot action is deprecated. "
                                "Please specify the kernel type in the deploy parameters.")

    def populate(self, parameters):
        self.internal_pipeline = Pipeline(parent=self, job=self.job, parameters=parameters)
        # customize the device configuration for this job
        self.internal_pipeline.add_action(UBootSecondaryMedia())
        self.internal_pipeline.add_action(BootloaderCommandOverlay())
        self.internal_pipeline.add_action(ConnectDevice())
        self.internal_pipeline.add_action(UBootRetry())


class UBootRetry(BootAction):

    name = "uboot-retry"
    description = "interactive uboot retry action"
    summary = "uboot commands with retry"

    def populate(self, parameters):
        self.internal_pipeline = Pipeline(parent=self, job=self.job, parameters=parameters)
        self.method_params = self.job.device['actions']['boot']['methods']['u-boot']['parameters']
        self.usb_mass_device = self.method_params.get('uboot_mass_storage_device', None)
        # establish a new connection before trying the reset
        self.internal_pipeline.add_action(ResetDevice())
        self.internal_pipeline.add_action(BootloaderInterruptAction())
        if self.method_params.get('uboot_ums_flash', False):
            self.internal_pipeline.add_action(BootloaderCommandsAction(expect_final=False))
            self.internal_pipeline.add_action(WaitDevicePathAction(self.usb_mass_device))
            self.internal_pipeline.add_action(FlashUBootUMSAction(self.usb_mass_device))
            self.internal_pipeline.add_action(ResetDevice())
        else:
            self.internal_pipeline.add_action(BootloaderCommandsAction())
        if self.has_prompts(parameters):
            self.internal_pipeline.add_action(AutoLoginAction())
            if self.test_has_shell(parameters):
                self.internal_pipeline.add_action(ExpectShellSession())
                if 'transfer_overlay' in parameters:
                    self.internal_pipeline.add_action(OverlayUnpack())
                self.internal_pipeline.add_action(ExportDeviceEnvironment())

    def validate(self):
        super(UBootRetry, self).validate()
        self.set_namespace_data(
            action=self.name,
            label='bootloader_prompt',
            key='prompt',
            value=self.job.device['actions']['boot']['methods']['u-boot']['parameters']['bootloader_prompt']
        )

    def run(self, connection, max_end_time, args=None):
        connection = super(UBootRetry, self).run(connection, max_end_time, args)
        self.set_namespace_data(action='shared', label='shared', key='connection', value=connection)
        return connection


class UBootSecondaryMedia(BootloaderSecondaryMedia):
    """
    Idempotent action which sets the static data only used when this is a boot of secondary media
    already deployed.
    """

    name = "uboot-from-media"
    description = "let uboot know where to find the kernel in the image on secondary media"
    summary = "set uboot strings for deployed media"

    def validate(self):
        if 'media' not in self.job.device.get('parameters', []):
            return
        media_keys = self.job.device['parameters']['media'].keys()
        if self.parameters['commands'] not in list(media_keys):
            return
        super(UBootSecondaryMedia, self).validate()
        if 'kernel_type' not in self.parameters:
            self.errors = "Missing kernel_type for secondary media boot"
        self.logger.debug("Mapping kernel_type: %s", self.parameters['kernel_type'])
        bootcommand = map_kernel_uboot(self.parameters['kernel_type'], self.job.device.get('parameters', None))
        self.logger.debug("Using bootcommand: %s", bootcommand)
        self.set_namespace_data(
            action='uboot-prepare-kernel', label='kernel-type',
            key='kernel-type', value=self.parameters.get('kernel_type', ''))
        self.set_namespace_data(
            action='uboot-prepare-kernel', label='bootcommand', key='bootcommand', value=bootcommand)

        media_params = self.job.device['parameters']['media'][self.parameters['commands']]
        if self.get_namespace_data(action='storage-deploy', label='u-boot', key='device') not in media_params:
            self.errors = "%s does not match requested media type %s" % (
                self.get_namespace_data(
                    action='storage-deploy', label='u-boot', key='device'), self.parameters['commands']
            )
        if not self.valid:
            return
        self.set_namespace_data(
            action=self.name,
            label='uuid',
            key='boot_part',
            value='%s:%s' % (
                media_params[self.get_namespace_data(action='storage-deploy', label='u-boot', key='device')]['device_id'],
                self.parameters['boot_part']
            )
        )


class UBootEnterFastbootAction(BootAction):

    name = "uboot-enter-fastboot"
    description = "interactive uboot enter fastboot action"
    summary = "uboot commands to enter fastboot mode"

    def __init__(self):
        super(UBootEnterFastbootAction, self).__init__()
        self.params = {}

    def populate(self, parameters):
        self.internal_pipeline = Pipeline(parent=self, job=self.job,
                                          parameters=parameters)
        # establish a new connection before trying the reset
        self.internal_pipeline.add_action(ResetDevice())
        # need to look for Hit any key to stop autoboot
        self.internal_pipeline.add_action(BootloaderInterruptAction())
        self.internal_pipeline.add_action(ConnectLxc())

    def validate(self):
        super(UBootEnterFastbootAction, self).validate()
        if 'u-boot' not in self.job.device['actions']['deploy']['methods']:
            self.errors = "uboot method missing"

        self.params = self.job.device['actions']['deploy']['methods']['u-boot']['parameters']
        if 'commands' not in self.job.device['actions']['deploy']['methods']['u-boot']['parameters']['fastboot']:
            self.errors = "uboot command missing"

    def run(self, connection, max_end_time, args=None):
        connection = super(UBootEnterFastbootAction, self).run(connection,
                                                               max_end_time,
                                                               args)
        connection.prompt_str = self.params['bootloader_prompt']
        self.logger.debug("Changing prompt to %s", connection.prompt_str)
        self.wait(connection)
        i = 1
        commands = self.job.device['actions']['deploy']['methods']['u-boot']['parameters']['fastboot']['commands']

        for line in commands:
            connection.sendline(line, delay=self.character_delay)
            if i != (len(commands)):
                self.wait(connection)
                i += 1

        return connection
