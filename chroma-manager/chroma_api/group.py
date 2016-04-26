#
# INTEL CONFIDENTIAL
#
# Copyright 2013-2016 Intel Corporation All Rights Reserved.
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


from django.contrib.auth.models import Group

from chroma_api.authentication import AnonymousAuthentication
from tastypie.authorization import ReadOnlyAuthorization

from chroma_api.chroma_model_resource import ChromaModelResource


class GroupResource(ChromaModelResource):
    """
    A user group.  Users inherit the permissions
    of groups of which they are a member.

    Groups are used internally to refer
    to factory-configured profiles, so this resource
    is read-only.
    """
    class Meta:
        authentication = AnonymousAuthentication()
        authorization = ReadOnlyAuthorization()
        queryset = Group.objects.all()
        filtering = {'name': ['exact', 'iexact']}
        ordering = ['name']

        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
