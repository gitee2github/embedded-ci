'''
Copyright (c) 2023 openEuler Embedded
oebuild is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details.
'''

import argparse
import os
import subprocess
import shutil
import fcntl
import json

import yaml

from app.command import Command
from app.lib import Gitee, Jenkins, Result
from app import util
from app.build import Build,BuildRes,BuildParam

class Gate(Command):
    '''
    Handle pull request business, including submission information checking, image building, etc 
    '''
    def __init__(self):
        self.jenkins = None
        self.gitee = None
        self.workspace = "/home/jenkins/agent"
        self.gate_share = None
        self.share_dir = None
        self.branch = None
        self.pr_num = None
        self.repo = None
        self.remote_url = None

        super().__init__(
            "gate", 
            "Handle pull request business", 
            "Handle pull request business, including submission information checking, image building, etc")

    def do_add_parser(self,parser_addr: argparse._SubParsersAction):
        parser = parser_addr.add_parser(name=self.name)
        parser.add_argument('-s', '--share_dir', dest = "share_dir")
        parser.add_argument('-o', '--owner', dest="owner")
        parser.add_argument('-p', '--repo', dest="repo")
        parser.add_argument('-gt', '--gitee_token', dest="gitee_token")
        parser.add_argument('-juser', '--jenkins_user', dest="jenkins_user")
        parser.add_argument('-jpwd', '--jenkins_pwd', dest="jenkins_pwd")
        parser.add_argument('-b', '--branch', dest="branch", default="master")
        parser.add_argument('-pr', '--pr_num', dest="pr_num")
        parser.add_argument('-is_test', '--is_test', dest = "is_test", action = "store_true")

        return parser

    def do_run(self, args, unknow):
        args = self.parser.parse_args(unknow)
        self.gate_share = os.path.join(args.share_dir, "gate")
        self.share_dir = args.share_dir
        self.branch = args.branch
        self.pr_num = args.pr_num

        self.gitee = Gitee(owner=args.owner, repo=args.repo, token=args.gitee_token)
        if not args.is_test:
            self.jenkins = Jenkins(jenkins_user=args.jenkins_user, jenkins_token=args.jenkins_pwd)
        self.repo = args.repo
        self.remote_url = f"https://gitee.com/{args.owner}/{args.repo}.git"

        self.exec(owner=args.owner, pr_num=args.pr_num, is_test=args.is_test)

    def send_build_link(self, pr_num, is_test:bool):
        '''
        sending user the build link
        '''
        build_url = ""
        if not is_test:
            build_url = os.path.join(os.environ['BUILD_URL'], 'console')
        comment = f"the gate is running, if you want to get message immediately, please click <a href='{build_url}'>here</a> for detail"
        self.gitee.comment_pr(pr_num=pr_num, comment=comment)

    def exec(self,owner,pr_num,is_test):
        '''
        the exec will be called by gate
        '''
        # first deal pre job
        if not is_test:
            self.delete_pre_jenkins(
                repo = self.repo,
                job_name = os.environ['JOB_NAME'],
                build_num = os.environ['BUILD_NUMBER'],
                owner = owner,
                pr_num = pr_num)
        # check pull request if docs
        if self.check_docs(pr_num=pr_num):
            return
        # send user gate link when task is starting
        self.send_build_link(pr_num=pr_num, is_test=is_test)
        # delete ci_progress tag in gitee
        self.gitee.delete_tags_of_pr(pr_num, "ci_successful", "ci_faild")
        self.gitee.add_tags_of_pr(pr_num, 'ci_processing')

        # first get pr commit list, and then clone pr with depth = len(commit)
        commit_data = self.gitee.get_pr_commits(pr_num=pr_num)
        commit_list = json.loads(commit_data)
        commit_hash_list = self._get_hash_from_commit(commit_list=commit_list)

        print(f"=============clone with pr {pr_num} ===========================")
        if os.path.exists(os.path.join(self.workspace, self.repo)):
            shutil.rmtree(os.path.join(self.workspace, self.repo))
        util.clone_repo_with_pr(
            src_dir=self.workspace,
            repo=self.repo,
            remote_url=self.remote_url,
            pr_num=pr_num,
            depth=len(commit_hash_list))
        repo_dir = os.path.join(self.workspace, self.repo)
        print(f"=============clone with pr {pr_num} finished===========================")

        # clone repo
        print("======================execute code check================================")
        code_check_res = self.code_check(repo_dir = repo_dir,commit_hash_list = commit_hash_list)
        print("======================code check finished================================")

        print("======================execute build check================================")
        build_res = self.build_check(repo_dir=repo_dir)
        print("======================build check finished================================")

        self.send_result(pr_num=pr_num, code_check_list=code_check_res, build_check_list=build_res, is_test=is_test)

    def code_check(self,repo_dir, commit_hash_list):
        '''
        execute code check and return result
        '''
        code = Code(repo_dir=repo_dir)
        return code.exec(commit_hash_list=commit_hash_list)

    def build_check(self, repo_dir):
        '''
        execute build check and return result
        '''
        gate_repo_path = os.path.join(util.get_top_path(), f"app/gate/{self.repo}/run.py")
        cls:Build = util.get_spec_ext(gate_repo_path, "Run")
        return cls.build(param=BuildParam(
            repo_dir=repo_dir,
            workspace=self.workspace,
            share_dir=self.share_dir,
            branch=self.branch,
            pr_num=self.pr_num))

    def send_result(self, pr_num, code_check_list, build_check_list:BuildRes, is_test: bool):
        '''
        format result to html table and send to gitee comment
        '''
        def format_code_check_list(code_check_list):
            format_code_check = {}
            final_res = Result().success
            for code in code_check_list:
                if code['result'] is Result().faild:
                    final_res = Result().faild
                format_code_check[code['name']] = f"{Result().get_emoji(code['result'])}\
                    <strong>{Result().get_hint(code['result'])}</strong>"
            return format_code_check, final_res

        def format_build_check_list(build_check_list:BuildRes):
            format_build_check = {}
            final_res = Result().success
            for arch in build_check_list.archs:
                arch_res = {}
                for build in arch.boards:
                    if build.result is Result().faild:
                        final_res = Result().faild
                    arch_res[build.name] = f"{Result().get_emoji(build.result)}\
                        <strong>{Result().get_hint(build.result)}</strong>"
                format_build_check[arch.name] = arch_res
            return format_build_check, final_res

        format_code_check, code_check_res = format_code_check_list(code_check_list)
        print(f"code_check: {format_code_check}, code_res: {code_check_res}")
        format_build_check, build_check_res = format_build_check_list(build_check_list)
        print(f"build_check: {format_build_check}, build_res: {build_check_res}")
        final_res = code_check_res or build_check_res


        comment = {"<strong>check name</strong>": "<strong>result</strong>"}
        for key, value in format_code_check.items():
            comment[key] = value
        for key, value in format_build_check.items():
            comment[key] = value
        comment = util.json_to_html(comment)
        # add an access link after comment table
        comment = comment + "\n"
        build_url = ""
        if not is_test:
            build_url = os.path.join(os.environ['BUILD_URL'], 'console')
        comment = comment + f"Please click <a href='{build_url}'>here</a> for details"
        self.gitee.comment_pr(pr_num=pr_num, comment=comment)

        # send check result tag to gitee
        self.gitee.delete_tags_of_pr(pr_num, 'ci_processing')
        if final_res is Result().success:
            self.gitee.add_tags_of_pr(pr_num, 'ci_successful')
        else:
            self.gitee.add_tags_of_pr(pr_num, 'ci_failed')

    def _get_hash_from_commit(self, commit_list):
        hash_list = []
        for commit in commit_list:
            hash_list.append(commit['sha'])
        return hash_list

    def check_docs(self, pr_num):
        '''
        xxx
        '''
        commits_files_data = self.gitee.get_commits_files(pr_num)
        commit_files_list = json.loads(commits_files_data)
        for file_obj in commit_files_list:
            if not file_obj['filename'].startswith("docs"):
                return False
        self.gitee.comment_pr(
            pr_num=pr_num,
            comment="This pull request only submits documentation, so no builds will take place")
        return True

    def delete_pre_jenkins(self, repo, job_name, build_num, owner, pr_num):
        '''
        delete jenkins job when exists
        '''
        pr_dir = os.path.join(self.gate_share, repo, owner, 'pr_num')
        if not os.path.exists(pr_dir):
            os.makedirs(pr_dir)
        pr_file = os.path.join(pr_dir, str(pr_num))
        if not os.path.exists(pr_file):
            os.mknod(pr_file)
        with open(pr_file, 'r', encoding='utf-8') as r_f:
            fcntl.flock(r_f.fileno(), fcntl.LOCK_EX)
            pr_data =  r_f.read()
            if pr_data != '':
                # stop pre jenkins job
                pr_json = yaml.load(pr_data, yaml.Loader)
                if "job_name" in pr_json and "build_num" in pr_json:
                    pre_build_info = self.jenkins.get_build_info(
                        job_name=pr_json['job_name'],
                        build_num=pr_json['build_num'])
                    if 'building' in pre_build_info and pre_build_info['building']:
                        comment = "you retrigger the gatekeeper, the previous access task will stop and then restart the new access mission"
                        self.gitee.comment_pr(pr_num=pr_num, comment=comment)
                        self.jenkins.stop_build_by_build_num(
                            job_name=pr_json['job_name'],
                            build_num=pr_json['build_num'])
            with open(pr_file, 'w', encoding='utf-8') as w_f:
                pr_data = {"job_name": job_name, 'build_num': build_num}
                yaml.dump(pr_data, w_f)

class Code:
    '''
    the code check will be executed commit check
    '''

    def __init__(self, repo_dir):
        self.repo_dir = repo_dir

    def exec(self, commit_hash_list: list):
        '''
        execute the code check
        '''
        code_res = []
        # check commit msg
        code_res.append({
            'name': 'check_commit_msg',
            'result': self.check_commit_msg(commit_hash_list=commit_hash_list)
            })
        # if has the other check do like up step
        return code_res

    def _get_gitlint_dir(self):
        return os.path.join(util.get_conf_path(), '.gitlint')

    def check_commit_msg(self, commit_hash_list):
        '''
        check commit msg and return result
        '''
        os.chdir(self.repo_dir)
        res = []
        for commit_hash in commit_hash_list:
            # get commit and add to dist for follow using
            command = f"gitlint --commit {commit_hash} -C {self._get_gitlint_dir()}"
            check_res = subprocess.getoutput(cmd=command)
            if check_res != "":
                res.append({'commit': commit_hash, 'result': check_res})

        if len(res) <= 0:
            return Result().success
        log_format = """
commit specifications is:
script: title

this is commit body

Signed-off-by: example example@xx.com
the folowint commits do not conform to the specifications:
        """
        print(log_format)
        print("==============================================================")
        for check_res in res:
            print("commit: %s", check_res['commit'])
            print("check result: \n\r %s", check_res['result'])
            print("==============================================================")
        return Result().faild
