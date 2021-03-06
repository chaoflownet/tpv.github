from requests import request
import sqlite3
import json
import os
import sys
import time
import pickle
from ConfigParser import ConfigParser
import re
from itertools import chain

from metachao import aspect
from metachao.classtree import CLASSTREE_ATTR
from tpv.ordereddict import OrderedDict
import tpv.generic

URL_BASE = 'https://api.github.com'


def merge_dicts(*dicts):
    return dict(chain(*(d.iteritems() for d in dicts)))


def set_on_new_dict(basedict, key, value):
    ret = basedict.copy()
    ret[key] = value
    return ret


# Read in some authentication data from ~/.ghconfig.
# It should contain a section like
#
#   [github]
#   user=<username>
#   token=<personal access token>
#
# where the personal access token can be generated with "Create new
# token" on https://github.com/settings/applications.


class RelativeDictionaryAccess(aspect.Aspect):
    """Aspect to enhance __getitem__ and __contains__, so they accept
point-delimited keys for multiple levels at once.

i.e. a["k1.k2.k3"] returns a["k1"]["k2"]["k3"]
    """
    @aspect.plumb
    def __getitem__(_next, self, key):
        try:
            for k in key.split("."):
                cur = _next(k)
                _next = cur.__getitem__
        except AttributeError:
            pass

        return cur

    @aspect.plumb
    def __contains__(_next, self, key):
        if "." not in key:
            return _next(key)

        nodes = key.split(".")
        curr = self
        try:
            for k in nodes[:-1]:
                curr = self[k]
        except KeyError:
            return False

        return nodes[-1] in curr


class DictConfigParser(object):
    """A config access object

All config files with the same `basename` between the current
directory and the home directory are parsed in order into a
config_dict dictionary tree.

config_dict = OrderedDict((file1,
                           OrderedDict((section1,
                                        { 'option1': 'value1',
                                          'option2': 'value2' }),
                                       (section2,
                                        { ... }),
                                       ...)),
                          (file2,
                           ...))

__getitem__, __contains__ and iteration-methods access to this
dictionary is provided across all files and returns the first matching
option (so that the config is taken from the most specific config
file).

the full dictionary with the extra file level can be accessed with the
by_files method.

Multiple levels of the config can be accessed by one call to
__getitem__ or __contains__ by using '.' as delimiter.
config["github.user"] -> config["github"]["user"]

Usage:

config = DictConfigParser(".ghconfig")
config["github.user"] -> returns user option from github section

config.by_files()["/home/coroa/.ghconfig"]["github.user"]
-> retrieves this user option from the config file in the home
directory specifically
    """

    def __init__(self, basename):
        """Locate and read in all files with basename from current directory
up to the home directory and build up the config dictionary `config_dict`
        """

        def get_files(basename):
            home = os.path.expanduser("~")
            path = os.getcwd()

            while path and path != home:
                yield os.path.join(path, basename)
                path = path[:path.rfind(os.path.sep)]

            yield os.path.join(home, basename)

        def parse_config(f):
            config = ConfigParser()
            config.read(f)
            return RelativeDictionaryAccess(
                OrderedDict((section,
                             dict((option, value)
                                  for option, value in config.items(section)))
                            for section in config.sections())
            )

        self.config_dict = OrderedDict((f, parse_config(f))
                                       for f in get_files(basename))

    def by_files(self):
        """Returns the full config dictionary with its filename level """
        return self.config_dict

    def __getitem__(self, key):
        """Return the first section or section.option match from the config
files
        """

        if "." in key:
            for config in self.config_dict.itervalues():
                if key in config:
                    return config[key]
            raise KeyError(key)
        else:
            # we have to merge all sections with `key`
            return self._flat_dict()[key]

    def __contains__(self, key):
        if "." in key:
            for config in self.config_dict.itervalues():
                if key in config:
                    return True
            return False
        else:
            # we have to merge all sections with `key`
            return key in self._flat_dict()

    def _flat_dict(self):
        """Return an OrderedDict without the extra file level, in which all
sections of the same name are merged.
        """
        ret = OrderedDict()
        for sec_name, sec_dict in \
            chain.from_iterable(x.iteritems()
                                for x in self.config_dict.values()):
            ret[sec_name] = (merge_dicts(sec_dict, ret[sec_name])
                             if sec_name in ret
                             else sec_dict)
        return RelativeDictionaryAccess(ret)

    def __iter__(self):
        return iter(self._flat_dict())

    iterkeys = __iter__

    def itervalues(self):
        return self._flat_dict().itervalues()

    def iteritems(self):
        return self._flat_dict().iteritems()

    def keys(self):
        return list(self)

    def values(self):
        return list(self.itervalues())

    def items(self):
        return list(self.iteritems())

# Read in config from all .ghconfig files
config = DictConfigParser(".ghconfig")


def authenticated_user():
    return config["github.user"]


def extract_repo_from_issue_url(url):
    """Returns (<owner>, <repo_name>) from the `url` of an issue """
    m = re.match(URL_BASE + "/repos/(.+)/(.+)/issues/", url)
    return (m.group(1), m.group(2))


def github_request(method, urlpath, data=None, params=None):
    """Request `urlpath` from github using authentication from config

    Arguments:
    - `method`: one of "HEAD", "GET", "POST", "PATCH", "DELETE"
    - `urlpath`: the path part of the request url, i.e. /users/coroa
    - `data`: POST/PATCH supplied arguments (dictionary)
    - `params`: GET parameters to be added to the url (dictionary)

    Returns a Request object for the call to github.
    """
    req = request(method, URL_BASE + urlpath,
                  auth=(config["github.user"],
                        config["github.token"]),
                  data=None
                  if data is None
                  else json.dumps(data),
                  params=params)

    if "github.debug" in config and int(config["github.debug"]) >= 2:
        sys.stderr.write(('''
>>> Request
{method} {url}
{reqbody}
>>> Response
{status}
{respbody}
        '''.strip()+"\n").format(
            method=req.request.method,
            url=req.request.url,
            reqbody=req.request.body,
            status=req.headers["status"],
            respbody=json.dumps(req.json(),
                                indent=2,
                                separators=(',', ': '))
        ))

    return req


def github_request_paginated(method, urlpath, params=None):
    """Generator, which yields all items of a multipage github request for
lists of objects.
    """
    while urlpath:
        req = github_request(method, urlpath, params=params)
        if '200 OK' not in req.headers['status']:
            raise RuntimeError(req.json()['message'])

        for elem in req.json():
            yield elem

        urlpath = None
        if "Link" in req.headers:
            m = re.search('<(https[^>]*)>; rel="next"', req.headers["Link"])
            if m:
                urlpath = m.group(1)[len(URL_BASE):]


def github_request_length(urlpath):
    """Return the number of items of a github request for lists of
objects.
    """
    req = github_request("GET", urlpath + "?per_page=1")
    m = re.search('<https[^>]*[?&]page=(\d+)[^>]*>; rel="last"',
                  req.headers["Link"])
    if m:
        return int(m.group(1))
    else:
        return 0


class GhBase(dict):
    """Base object for a node in the github dictionary tree

Sets the parent of a node. Sets the supplied parameters or fetches
them from the parent. Takes care of making the parameters accessable
as attributes of the node with an underscore as prefix.
    """

    # class used for caching nodes of this type
    # (consulted by the cache aspect from tpv.generic)
    cache_class = dict

    def __init__(self, parent, data=None, **kwargs):
        self.init_sqlite()

        self._parent = parent
        self._parameters = kwargs

        if not self._parameters and self._parent:
            self._parameters = self._parent._parameters

        for k, v in self._parameters.iteritems():
            setattr(self, "_" + k, v)

    def __repr__(self):
        """Return a human-readable presentation of the instance

Includes the classname and the parameters with their values. """
        return "<{} [{}]>".format(
            self.__class__.__name__,
            " ".join("{}={}".format(k, v)
                     for k, v in self._parameters.iteritems())
        )

    def _debug(self, func, *args):
        if "github.debug" in config and int(config["github.debug"]) >= 1:
            sys.stderr.write("{}.{}({})\n".format(self, func, ", ".join(args)))

    def add(self, **arguments):
        raise NotImplementedError("Nothing to see here, move along")

    expiral_time = 24*60*60

    @classmethod
    def init_sqlite(cls):
        if hasattr(cls, "sqlite"):
            return

        try:
            filepath = config["Cache DB.filepath"]
        except KeyError:
            filepath = "/tmp/githubcache.db"

        cls.sqlite = sqlite3.connect(filepath,
                                     isolation_level=None)

        cls.sqlite.execute("create table if not exists cache"
                           "(identifier text, parameters text,"
                           " expires integer, data blob,"
                           " primary key(identifier, parameters))")

    @classmethod
    def clear_cache(cls):
        cls.sqlite.execute("delete from cache")

    def serialize(self, identifier=None):
        '''Save dictionary items into sqlite table

Expiral time is set in seconds via config section "Expiral time" in
__class__.__name__ options. It falls back to attribute `expiral_time`
(a day).

a joined version of self._parameters is used as secondary key.

`identifier` -- String usually composed of class name
                and perhaps a suffix like "_partial"
        '''

        if identifier is None:
            identifier = self.__class__.__name__

        parameters = ",".join("{}={}".format(k, v)
                              for k, v in self._parameters.iteritems())

        try:
            expiral_time = config["Expiral time"][self.__class__.__name__]
        except KeyError:
            expiral_time = self.expiral_time

        data = super(GhBase, self).items()
        self.sqlite.execute("insert or replace into cache values (?,?,?,?)",
                            (identifier, parameters,
                             int(time.time() + expiral_time),
                             buffer(pickle.dumps(data))))

    def deserialize(self, identifier=None):
        if identifier is None:
            identifier = self.__class__.__name__

        self.sqlite.execute("delete from cache where expires < ?",
                            (time.time(),))
        c = self.sqlite.cursor()

        parameters = ",".join("{}={}".format(k, v)
                              for k, v in self._parameters.iteritems())
        c.execute('select data from cache where identifier=? and parameters=?',
                  (identifier, parameters))
        row = c.fetchone()
        if row is None:
            return False
        else:
            data = pickle.loads(row[0])

            super(GhBase, self).update(data)
            return True


class GhResource(GhBase):
    """Base class for nodes representing a single object/a resource

Can receive its data or fetch it on its own in __init__ using the
attribute url_template (to be specified in child classes).

It provides in addition to the usual dictionary access, an update
function to change multiple attributes with a single call to github.
    """

    @property
    def url_template(self):
        """Template to construct the github url for the resource

placeholders will be filled from _parameters
(f.ex. "/users/{user}" -> "/users/octocat").

should be overwritten by child classes.
        """
        raise NotImplementedError()

    def __init__(self, parent, data=None, **kwargs):
        super(GhResource, self).__init__(parent, data, **kwargs)

        if data is not None:
            self._is_partial = True
            super(GhResource, self).update(data)
            self.serialize()
        else:
            # self.complete_data raises ValueError if it couldn't
            # fetch the resource
            self.deserialize() or self.complete_data()

    def serialize(self):
        super(GhResource, self).serialize(self.__class__.__name__ +
                                          ("_partial"
                                           if self._is_partial else ""))

    def deserialize(self):
        if super(GhResource, self).deserialize(self.__class__.__name__):
            self._is_partial = False
            return True
        elif super(GhResource, self).deserialize(self.__class__.__name__
                                                 + "_partial"):
            self._is_partial = True
            return True

        return False

    def complete_data(self):
        url = self.url_template.format(**self._parameters)
        req = github_request("GET", url)

        if '200 OK' not in req.headers["status"]:
            raise ValueError("Couldn't fetch {} object: {}"
                             .format(self, req.json()["message"]))

        super(GhResource, self).update(req.json())
        self._is_partial = False
        self.serialize()
        return True

    def __getitem__(self, key):
        self._debug("__getitem__", key)
        try:
            return super(GhResource, self).__getitem__(key)
        except KeyError:
            if self._is_partial:
                self.complete_data()
                return super(GhResource, self).__getitem__(key)
            else:
                raise

    def __setitem__(self, key, value):
        self._debug("__setitem__", key, value)
        self.update({key: value})

    def update(self, data):
        try:
            # for PATCH updates github requires the list_key (the
            # attribute name for identifying the object) to be set
            if self._parent.list_key not in data:
                data = set_on_new_dict(data,
                                       self._parent.list_key,
                                       self._parameters[self._parent.child_parameter])
        except NotImplementedError:
            # the parent is not iterable, the caller of update has to
            # supply all mandatory arguments in data
            pass

        url = self.url_template.format(**self._parameters)
        req = github_request("PATCH", url, data=data)
        if '200 OK' not in req.headers["status"]:
            raise ValueError("Couldn't update {} object: {}"
                             .format(self.__class__.__name__,
                                     req.json()["message"]))

        # update the cached data with the live data from
        # github. a detailed representation.
        super(GhResource, self).update(req.json())

        self.serialize()


class GhCollection(GhBase):
    """Base class for nodes representing a collection/a list of resources

It provides in addition to the usual dictionary access, a search
function for accessing a subset of resources and an add function to
create new resources.

Attributes for defining a collection are:

child_class, child_parameter -- class and parameter name for the resources
get_url_template             -- for accessing a single resource
list_url_template, list_key  -- for iteration.
add_url_template, add_method, add_required_arguments
                             -- for adding new resources
delete_url_template          -- for deleting resources
    """

    @property
    def child_class(self):
        """Class of a resource """
        raise NotImplementedError()

    @property
    def child_parameter(self):
        """Parameter name identifying a resource """
        raise NotImplementedError()

    @property
    def list_url_template(self):
        """Template to construct the url to iterate the collection """
        raise NotImplementedError("Collection is not iterable.")

    @property
    def list_key(self):
        """Attribute which identifies a resource on the github side """
        raise NotImplementedError("Collection is not iterable.")

    @property
    def add_url_template(self):
        """Template to construct the url to create a new resource """
        raise NotImplementedError("Can't add to collection.")

    # Method used to create new resources, defaults to "POST"
    add_method = "POST"
    # Arguments required to add a resource (checked before calling github)
    add_required_arguments = []

    @property
    def delete_url_template(self):
        """Template to construct the url to delete a resource """
        raise NotImplementedError("Can't delete from collection.")

    def __init__(self, parent, data=None, **parameters):
        super(GhCollection, self).__init__(parent, data=data, **parameters)
        self.deserialize()

    def search(self, **arguments):
        """Query github for a subset of resources

Parameters:
`**arguments` -- keyword filters passed through to github

Returns (<key>, GhResource()) tuples of the resources matching arguments.
        """
        def item(key, data=None):
            return (key,
                    self.child_class(self,
                                     data=data,
                                     **set_on_new_dict(self._parameters,
                                                       self.child_parameter,
                                                       key)))

        if len(arguments) > 0:
            for x in self._get_resources(**arguments):
                yield item(x[self.list_key], x)
        elif super(GhCollection, self).__len__() > 0 \
            and len([x for x in super(GhCollection, self).itervalues()
                     if x is None]) == 0:
            for x in super(GhCollection, self).iterkeys():
                yield item(x)
        else:
            keys_candidate = []
            for x in self._get_resources(**arguments):
                keys_candidate.append((x[self.list_key], 'partial'))
                yield item(x[self.list_key], x)
            super(GhCollection, self).update(keys_candidate)
            self.serialize()

    def _get_resources(self, **arguments):
        """Query github for all or a subset of resources

Returns a generator to iterate over all matching github resources.
        """
        url = self.list_url_template.format(**self._parameters)
        return github_request_paginated("GET", url, params=arguments)

    def iterkeys(self):
        if super(GhCollection, self).__len__() > 0:
            for x in super(GhCollection, self).iterkeys():
                yield x
        else:
            keys_candidate = []
            for x in self._get_resources():
                keys_candidate.append((x[self.list_key], None))
                yield x[self.list_key]
            super(GhCollection, self).update(keys_candidate)
            self.serialize()

    __iter__ = iterkeys

    def keys(self):
        return list(self.iterkeys())

    def itervalues(self):
        return (x[1] for x in self.iteritems())

    def values(self):
        return list(self.itervalues())

    def iteritems(self):
        return self.search()

    def items(self):
        return list(self.iteritems())

    def __len__(self):
        return len(self.keys())

    def __getitem__(self, key):
        """Return the GhResource object for `key` """
        self._debug("__getitem__", key)

        parameters = set_on_new_dict(self._parameters,
                                     self.child_parameter,
                                     key)

        try:
            return self.child_class(self, **parameters)
        except ValueError:
            raise KeyError(key)

    def add(self, **arguments):
        """Create a new resource """

        self._debug("add", *("{}={}".format(k, v)
                             for k, v in arguments.iteritems()))

        # check if all required arguments are provided
        for required_arg in self.add_required_arguments:
            if required_arg not in arguments:
                raise ValueError("Not all required arguments {} provided"
                                 .format(", ".join(self.add_required_arguments)))

        if self.add_method == "POST":
            url = self.add_url_template.format(**self._parameters)
            req = github_request("POST", url,
                                 data=arguments)
            if "201 Created" not in req.headers["status"]:
                raise ValueError("Couldn't create {} object: {}"
                                 .format(self.child_class.__name__,
                                         req.json()["message"]))
            else:
                # return the new resource
                data = req.json()
                super(GhCollection, self).__setitem__(data[self.list_key],
                                                      'partial')
                self.serialize()

                parameters = set_on_new_dict(self._parameters,
                                             self.child_parameter,
                                             data[self.list_key])
                return self.child_class(parent=self, data=data, **parameters)

        elif self.add_method == "PUT":
            # For PUT requests the child_parameter is already part of
            # the url. Thus it has to be taken from arguments for
            # constructing the url.
            if self.list_key not in arguments:
                raise ValueError("The required argument {} was not provided"
                                 .format(self.list_key))
            tmpl_vars = set_on_new_dict(self._parameters,
                                        self.child_parameter,
                                        arguments[self.list_key])
            url = self.add_url_template.format(**tmpl_vars)

            req = github_request("PUT", url,
                                 data=arguments)
            if "204 No Content" not in req.headers["status"]:
                raise ValueError("Couldn't create {} object: {}"
                                 .format(self.child_class.__name__,
                                         req.json()["message"]))
            # PUT requests don't return any content

            super(GhCollection, self).__setitem__(arguments[self.list_key],
                                                  None)
            self.serialize()

    def __delitem__(self, key):
        """Delete resource from collection """
        self._debug("__delitem__", key)

        tmpl_vars = set_on_new_dict(self._parameters,
                                    self.child_parameter, key)
        url = self.delete_url_template.format(**tmpl_vars)
        req = github_request("DELETE", url)
        if "204 No Content" not in req.headers["status"]:
            raise ValueError("Couldn't delete {} object: {}"
                             .format(self.child_class.__name__,
                                     req.json()["message"]))

        try:
            super(GhCollection, self).__delitem__(key)
            self.serialize()
        except KeyError:
            pass


class cache(tpv.generic.cache):
    @aspect.plumb
    def add(_next, self, **arguments):
        ret = _next(**arguments)

        if self.cache_keys is None:
            pass
        elif self.add_method == "POST":
            self.cache_keys.append(ret[self.list_key])
        elif self.add_method == "PUT":
            self.cache_keys.append(arguments[self.list_key])

        return ret

    def _wrap_child(self, child):
        cls = child.__class__
        child = cache(cls, cache=self._get_cache(child))(
            parent=self,
            data=child.iteritems() if isinstance(child, GhResource) else None,
            **child._parameters
        )

        # TODO: change classtree, so they work with aspects
        # workaround: copy over classtree child relations
        # as the node metaclass init function is called on the derived
        # aspect class, which clears the attribute.
        try:
            setattr(child.__class__, CLASSTREE_ATTR,
                    getattr(cls, CLASSTREE_ATTR))
        except AttributeError:
            pass

        return child
