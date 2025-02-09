# -*- coding: utf-8 -*-
#
# Copyright (C) 2004-2022 Edgewall Software
# Copyright (C) 2004-2005 Jonas Borgström <jonas@edgewall.com>
# Copyright (C) 2004-2005 Daniel Lundin <daniel@edgewall.com>
# Copyright (C) 2005-2006 Christopher Lenz <cmlenz@gmx.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.
#
# Author: Jonas Borgström <jonas@edgewall.com>
#         Christopher Lenz <cmlenz@gmx.de>

import re

from trac.config import get_configinfo
from trac.core import *
from trac.loader import get_plugin_info
from trac.perm import IPermissionRequestor
from trac.util.html import tag
from trac.util.translation import _
from trac.web.api import IRequestHandler
from trac.web.chrome import Chrome, INavigationContributor, accesskey


class AboutModule(Component):
    """"About Trac" page provider, showing version information from
    third-party packages, as well as configuration information."""

    required = True

    implements(INavigationContributor, IPermissionRequestor, IRequestHandler)

    # INavigationContributor methods

    def get_active_navigation_item(self, req):
        return 'about'

    def get_navigation_items(self, req):
        yield ('metanav', 'about',
               tag.a(_("About Trac"), href=req.href.about(),
                     accesskey=accesskey(req, 9)))

    # IPermissionRequestor methods

    def get_permission_actions(self):
        return ['CONFIG_VIEW']

    # IRequestHandler methods

    def match_request(self, req):
        return re.match(r'/about(?:_trac)?$', req.path_info)

    def process_request(self, req):
        data = {'systeminfo': None, 'plugins': None,
                'config': None, 'interface': None}

        if 'CONFIG_VIEW' in req.perm('config', 'systeminfo'):
            # Collect system information
            data['system_info'] = self.env.system_info
            Chrome(self.env).add_jquery_ui(req)

        if 'CONFIG_VIEW' in req.perm('config', 'plugins'):
            # Collect plugin information
            data['plugins'] = get_plugin_info(self.env)

        if 'CONFIG_VIEW' in req.perm('config', 'interface'):
            data['interface'] = \
                Chrome(self.env).get_interface_customization_files()

        if 'CONFIG_VIEW' in req.perm('config', 'ini'):
            # Collect config information
            data['config'] = get_configinfo(self.env)

        return 'about.html', data
