#
# INTEL CONFIDENTIAL
#
# Copyright 2013-2015 Intel Corporation All Rights Reserved.
#
# The source code contained or described herein and all documents related
# to the source code ("Material") are owned by Intel Corporation or its
# suppliers or licensors. Title to the Material remains with Intel Corporation
# or its suppliers and licensors. The Material contains trade secrets and
# proprietary and confidential information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright and trade secret
# laws and treaty provisions. No part of the Material may be used, copied,
# reproduced, modified, published, uploaded, posted, transmitted, distributed,
# or disclosed in any way without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be
# express and approved by Intel in writing.


from tastypie.utils import trailing_slash
from tastypie.api import url
from tastypie.constants import ALL_WITH_RELATIONS
from tastypie import fields, http

from chroma_core.models.event import Event
from chroma_core.services.dbutils import advisory_lock
from chroma_api.utils import SeverityResource
from chroma_api.authentication import AnonymousAuthentication, \
    PATCHSupportDjangoAuth


class EventResource(SeverityResource):
    """
    An event is a message generated by the manager server to indicate a
    change on a host or in a file system being monitored. Events are similar
    to log messages, but do not appear in the log resource because they are
    internally synthesized rather than received via syslog.
    """

    host_name = fields.CharField(
        blank = True,
        null = True,
        help_text = ("The ``label`` attribute of the host on which the event "
                    "occurred, or null if the event is not specific to a "
                    "single host"))

    host = fields.ToOneField(
        'chroma_api.host.HostResource', 'host', null = True,
        help_text = ("The host on which the event occurred, or null if the "
                     "event is not specific to a single host"))

    message = fields.CharField(help_text = ("Human readable description "
                                            "of the event, about one sentence"))

    class Meta:
        queryset = Event.objects.all()
        authorization = PATCHSupportDjangoAuth()
        authentication = AnonymousAuthentication()
        ordering = ['created_at', 'host', 'host_name']
        filtering = {
                'severity': ['exact', 'in'],
                'host': ALL_WITH_RELATIONS,
                'created_at': ['gte', 'lte', 'gt', 'lt'],
                'dismissed': ['exact']
                }
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get', 'patch']
        always_return_data = True

    def override_urls(self):
        return [
            url(r'^(?P<resource_name>%s)/dismiss_all%s$' % (self._meta.resource_name, trailing_slash()), self.wrap_view('dismiss_all'), name='api_event_dismiss_all'),
        ]

    def dismiss_all(self, request, **kwargs):
        if (request.method != 'PUT') or (not request.user.is_authenticated()):
            return http.HttpUnauthorized()

        Event.objects.filter(dismissed = False).update(dismissed = True)

        return http.HttpNoContent()

    def get_object_list(self, request):
        return Event.objects.all()

    def dehydrate_host_name(self, bundle):
        """When sending to API caller, initialize this field. """

        return bundle.obj.host.get_label() if bundle.obj.host else "---"

    def dehydrate_message(self, bundle):
        return bundle.obj.message()

    def hydrate_host(self, bundle):
        """Check the host isn't deleted, if it exists"""

        #  NB:  This works because even when an object is "deleted", it is
        #  possible navigate with dot notation to the object.  If that is
        #  ever changed, this will fail.  But a test will catch that.
        if bundle.obj.host is None or bundle.obj.host.not_deleted is None:
            #  don't let Tasty load it.
            bundle.data['host'] = None
        return bundle

    def build_filters(self, filters = None):
        """Convert HTTP param incoming values to DB types in the filter."""

        custom_filters = {}

        #  Force event_type to lower case
        event_type = filters.get('event_type', None)
        if event_type:
            del filters['event_type']
            custom_filters['content_type__model'] = event_type.lower()

        filters = super(EventResource, self).build_filters(filters)
        filters.update(custom_filters)
        return filters

    @advisory_lock(Event)
    def dispatch(self, request_type, request, **kwargs):
        return super(EventResource, self).dispatch(request_type, request, **kwargs)