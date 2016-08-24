import os
import shutil
import yaml
from distutils.dir_util import copy_tree

from nailgun.db import db
from nailgun.objects import NailgunObject, NailgunCollection, Cluster
from nailgun.objects.serializers.base import BasicSerializer
from nailgun.logger import logger
from nailgun.errors import errors

from git import Repo
from git import exc

from fuel_external_git.models import GitRepo
from fuel_external_git import const


class GitRepoSerializer(BasicSerializer):
    fields = (
        "id",
        "repo_name",
        "env_id",
        "git_url",
        "ref",
        "user_key"
    )


class GitRepo(NailgunObject):
    model = GitRepo
    serializer = GitRepoSerializer

    @classmethod
    def get_by_cluster_id(self, cluster_id):
        instance = db().query(self.model).\
                              filter(self.model.env_id == cluster_id).\
                              first()
        if instance is not None:
            try:
                instance.repo = Repo(os.path.join(const.REPOS_DIR,
                                                  instance.repo_name))
            except exc.NoSuchPathError:
                # TODO(dukov) Put some logging here
                instance.repo = GitRepo.clone(instance.git_url)
        return instance

    @classmethod
    def create(self, data):
        if not os.path.exists(const.REPOS_DIR):
            os.mkdir(const.REPOS_DIR)
        repo_path = os.path.join(const.REPOS_DIR, data['repo_name'])
        if os.path.exists(repo_path):
            # TODO(dukov) add some logging here
            shutil.rmtree(repo_path)

        self._create_key_file(data['repo_name'], data['key'])
        os.environ['GIT_SSH_COMMAND'] = self._get_ssh_cmd(data['repo_name'])
        repo = Repo.clone_from(data['git_url'], repo_path)

        instance = super(GitRepo, self).create(data)
        instance.repo = repo
        return instance

    @classmethod
    def checkout(self, instance):
        ssh_cmd = self._get_ssh_cmd(instance.repo_name)

        if not os.path.exists(self._get_key_path(instance.repo_name)):
            # TODO(dukov) put some logging here
            self._create_key_file(instance.repo_name)

        with instance.repo.git.custom_environment(GIT_SSH_COMMAND=ssh_cmd):
            commit = instance.repo.remotes.origin.fetch(refspec=instance.ref)
            commit = commit[0].commit
            instance.repo.head.reference = commit
            instance.repo.head.reset(index=True, working_tree=True)

    @classmethod
    def init(self, instance):
        overrides = {
                'nodes': {},
                'roles': {}
        }
        repo_path = os.path.join(const.REPOS_DIR, instance.repo_name)
        templates_dir = os.path.join(os.path.dirname(__file__),
                                     'templates', 'gitrepo')
        overrides_path = os.path.join(repo_path, 'overrides.yaml')

        try:
            self.checkout(instance)
        except exc.GitCommandError, e:
            logger.debug(("Remote returned following error {}. "
                          "Seem remote has not been initialised. "
                          "Skipping checkout".format(e)))

        cluster = Cluster.get_by_uid(instance.env_id)
        for node in cluster.nodes:
            overrides['nodes'][node.uid] = "node_{}_configs".format(node.uid)
            for role in node.all_roles:
                overrides['roles'][role] = role + '_configs'

        if not os.path.exists(overrides_path):
            with open(os.path.join(repo_path, 'overrides.yaml'), 'w') as fd:
                yaml.dump(overrides, fd, default_flow_style=False)
        else:
            # TODO (dukov) put some logging here
            pass

        copy_tree(templates_dir, repo_path)
        if instance.repo.is_dirty(untracked_files=True):
            instance.repo.git.add('-A')
            instance.repo.git.commit('-m "Config repo Initialized"')

            ssh_cmd = self._get_ssh_cmd(instance.repo_name)
            with instance.repo.git.custom_environment(
                    GIT_SSH_COMMAND=ssh_cmd):
                res = instance.repo.remotes.origin.push(
                        refspec='HEAD:' + instance.ref)
                logger.debug("Push result {}".format(res[0].flags))
                if res[0].flags not in (2, 256):
                    logger.debug("Push error. Result code should be 2 or 256")
                    if res[0].flags == res[0].UP_TO_DATE:
                        raise errors.NoChanges
                    else:
                        raise errors.UnresolvableConflict

    @classmethod
    def _create_key_file(self, repo_name, data):
        key_path = self._get_key_path(repo_name)
        with open(key_path, 'w') as key_file:
            key_file.write(data)
        os.chmod(key_path, 0o600)

    @classmethod
    def _get_key_path(self, repo_name):
        return os.path.join(const.REPOS_DIR, repo_name + '.key')

    @classmethod
    def _get_ssh_cmd(self, repo_name):
        key_path = self._get_key_path(repo_name)
        return 'ssh -o StrictHostKeyChecking=no -i ' + key_path


class GitRepoCollection(NailgunCollection):
    single = GitRepo
