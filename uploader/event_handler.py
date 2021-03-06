import os
import json
from time import time
from copy import deepcopy
from queue import Queue
from tempfile import gettempdir
from uploader.hashutils import hash_file, hash_string
from concurrent.futures import ThreadPoolExecutor
from threading import Thread, Lock
from watchdog.events import FileSystemEventHandler
from uploader.drive_service import DriveService
from uploader.notification import *
from googleapiclient.errors import HttpError
from pprint import pprint

FILES_BLACKLIST = set([".DS_Store", "__Sync__"])
PREFIX_BLACKLIST = set([".tmp"])


def folder_doc(id, pid, name, path):
    return {"id": id,
            "pid": pid,
            "name": name,
            "folder": True,
            "path": path}


def file_doc(id, pid, name, path, size, last_modified):
    return {"id": id,
            "pid": pid,
            "name": name,
            "folder": False,
            "last_modified": last_modified,
            "size": size,
            "path": path}


class DirectoryChangeEventHandler(FileSystemEventHandler, Thread):
    def __init__(self, base_folder_gid, root_path, notification_queue: Queue):
        self.base_folder_gid = base_folder_gid
        self.root_path = root_path
        self.notification_queue = notification_queue
        self.last_tree_id_file = hash_string(
            root_path.encode("utf-8")) + "_last.json"
        self.current_tree = self.load_last_tree()
        self.event_queue = Queue()
        self.uploader = ThreadPoolExecutor(max_workers=10)
        self.downloader = ThreadPoolExecutor(max_workers=10)
        self.service = DriveService()
        self.tree_lock = Lock()
        self.scheduled_for_upload = set()
        self.broken_files = set()
        super().__init__()

    def on_any_event(self, event):
        self.event_queue.put(event)

    def stop(self):
        self.event_queue.put(None)
        self.service.cancel_all()

    def run(self):
        # processes queue but debounces the changes since it always goes over the whole tree
        while True:
            try:
                if self.event_queue.get(timeout=.5) is None:
                    break
            except:
                self.process_event()
                if self.event_queue.get() is None:
                    break

    def clean_tree(self):
        to_clean = list(
            filter(lambda x: "downloading" in x, self.current_tree.values()))
        for node in to_clean:
            try:
                os.remove(node["path"])
            except:
                pass
            del self.current_tree[str(node["id"])]

    def process_event(self):
        old_tree = deepcopy(self.current_tree)
        tree_analysis = self.analyze_tree()

        # first upload folders then files will have the folder gid to be uploaded to
        self.current_tree = self.upload_folders(
            tree_analysis["new_folders"], tree_analysis["current_tree"])
        self.update_tree(self.current_tree)
        self.move_files(tree_analysis["moved_files"],
                        tree_analysis["old_tree"], self.current_tree)
        self.update_tree(self.current_tree)

        to_upload = {*tree_analysis["new_files"],
                     *tree_analysis["modified_files"],
                     *tree_analysis["still_not_uploaded_files"]}
        to_upload -= self.scheduled_for_upload
        self.scheduled_for_upload = {*self.scheduled_for_upload, *to_upload}
        self.upload_files(to_upload, self.current_tree)
        self.update_tree(self.current_tree)

        for file_id in tree_analysis["deleted_files"]:
            self.notification_queue.put(
                FileDeletedNotification(old_tree[file_id]))

    def remote_to_local_path(self, file_gid, remote_tree):
        if file_gid not in remote_tree.keys():
            return

        remote_file_node = remote_tree[file_gid]
        if "gpid" not in remote_file_node.keys():
            return

        local_gid_to_node = {
            node["gid"]: node for node in self.current_tree.values() if "gid" in node.keys()}

        remote_parent_node = remote_tree[remote_file_node["gpid"]]
        if remote_parent_node["id"] in local_gid_to_node.keys():
            local_parent_node = local_gid_to_node[remote_parent_node["id"]]
            local_path = local_parent_node["path"] + "/" + \
                local_parent_node["name"] + "/" + remote_file_node["name"]
            return local_parent_node, [
                {"name": remote_file_node["name"], "path": local_path,
                    "folder": False, "gid": file_gid}
            ]

        path_to_append = [remote_parent_node["name"]]
        nodes_to_create = [remote_parent_node]
        while True:
            remote_parent_node = remote_tree[remote_parent_node["gpid"]]
            path_to_append.append(remote_parent_node["name"])
            nodes_to_create.append(remote_parent_node)
            if remote_parent_node["id"] in local_gid_to_node.keys():
                break

        path_to_append = path_to_append[::-1]
        nodes_to_create = nodes_to_create[::-1]
        local_parent_node = local_gid_to_node[remote_parent_node["id"]]
        print("PATH:", path_to_append)
        print("NODES:", nodes_to_create)
        print("LOCAL PARENT:", local_parent_node)
        nodes_to_create_info = []
        for i in range(len(nodes_to_create)):
            node = nodes_to_create[i]
            path = local_parent_node["path"]
            if i > 0:
                path += "/" + "/".join(path_to_append[:i])
            print("PATH FOR:", node["name"], path)
            nodes_to_create_info.append(
                {"name": node["name"], "path": path, "folder": True, "gid": node["id"]})

        path = local_parent_node["path"] + "/" + \
            "/".join(path_to_append) + "/" + remote_file_node["name"]
        nodes_to_create_info.append(
            {"name": remote_file_node["name"], "path": path, "folder": False, "gid": file_gid})

        return local_parent_node, nodes_to_create_info

    def prepare_download(self, file_gid, remote_tree):
        local_parent, to_create = self.remote_to_local_path(
            file_gid, remote_tree)
        to_create_folders = [node for node in to_create if node["folder"]]
        to_create_file = list(filter(
            lambda node: not node["folder"], to_create))
        if len(to_create_file) != 1:
            return

        to_create_file = to_create_file[0]
        folder_pid = local_parent["id"]
        with self.tree_lock:
            for folder in to_create_folders:
                folder_id = hash_string(
                    (folder["path"] + "/" + folder["name"]).encode("utf-8"))
                if folder_id in self.current_tree.keys():
                    continue
                self.current_tree[folder_id] = folder_doc(
                    folder_id, folder_pid, folder["name"], folder["path"])
                self.current_tree[folder_id]["gid"] = folder["gid"]
                folder_pid = folder_id

            # create empty file just to get a inode
            tmp_path = gettempdir() + "/" + to_create_file["name"]
            open(tmp_path, "w").close()
            file_id = str(os.stat(tmp_path).st_ino)
            to_create_file["id"] = str(file_id)
            self.current_tree[file_id] = file_doc(
                file_id, folder_pid, to_create_file["name"], to_create_file["path"], 0, 0)
            self.current_tree[file_id]["gid"] = file_gid
            self.current_tree[file_id]["downloading"] = True
        self.save_tree(self.current_tree)

        # create deepest folder
        if len(to_create_folders) > 0:
            os.makedirs(to_create_folders[-1]["path"] + "/" +
                        to_create_folders[-1]["name"], exist_ok=True)
        os.rename(tmp_path, to_create_file["path"])

        return to_create_file

    def download_file(self, file_gid, to_create_file):
        download_progress_notification = Queue()

        def notify_progress():
            status = download_progress_notification.get()
            while status:
                self.notification_queue.put(
                    FileDownloadProgressNotification(
                        deepcopy(to_create_file),
                        status["progress"]),
                    False)
                status = download_progress_notification.get()

        Thread(target=notify_progress).start()

        self.service.download_file(
            file_gid, to_create_file["path"], download_progress_notification)
        self.current_tree[to_create_file["id"]]["downloading"] = False
        self.save_tree(self.current_tree)

    def add_gid(self, doc_id, gid):
        if doc_id not in self.current_tree.keys():
            print("There is no %s in the current tree." % doc_id)
            return

        with self.tree_lock:
            self.current_tree[doc_id]["gid"] = gid
            self.save_tree(self.current_tree)

    def update_tree(self, tree):
        with self.tree_lock:
            self.current_tree = tree
            self.save_tree(tree)

    def save_tree(self, tree):
        temp_file_name = hash_string(
            ("%s_%s" % (self.root_path, str(int(time())))).encode("utf-8")) + ".json"
        with open(temp_file_name, "w") as lt:
            lt.write(json.dumps(tree, indent=4))

        os.replace(temp_file_name, self.last_tree_id_file)

    def check_blacklists(self, filename):
        if filename in FILES_BLACKLIST:
            return True

        for prefix in PREFIX_BLACKLIST:
            if filename.startswith(prefix):
                return True

        return False

    def get_tree(self, path):
        new_tree = {}
        for root, dirs, files in os.walk(self.root_path):
            try:
                folder_id = hash_string(os.path.abspath(root).encode("utf-8"))
                folder_path = os.sep.join(root.split(os.sep)[:-1])
                parent_id = hash_string(folder_path.encode("utf-8"))
                folder_name = root.split(os.sep)[-1]
                new_tree[folder_id] = folder_doc(
                    folder_id, parent_id, folder_name, folder_path)

                for filename in files:
                    if self.check_blacklists(filename):
                        continue

                    try:
                        file_path = folder_path + "/" + folder_name + "/" + filename
                        if file_path in self.broken_files:
                            continue

                        stats = os.stat(file_path.encode("utf-8"))
                        file_id = stats.st_ino
                        last_modified = stats.st_mtime
                        file_size = stats.st_size
                        new_tree[str(file_id)] = file_doc(
                            file_id, folder_id, filename, file_path, file_size, last_modified)
                    except Exception as e:
                        # print("error on getting info for file:", e)
                        self.broken_files.add(file_path)
            except Exception as e:
                print("error while walking through tree:", e)
        return new_tree

    def analyze_tree(self):
        dowloading_files = set(filter(
            lambda k: "downloading" in self.current_tree[k], self.current_tree.keys()))
        new_tree = self.get_tree(self.root_path)
        new_files = new_tree.keys() - self.current_tree.keys()
        deleted_files = set(filter(
            lambda x: x not in dowloading_files, self.current_tree.keys() - new_tree.keys()))
        new_folders = set([f for f in new_files if new_tree[f]["folder"]] +
                          [f for f in self.current_tree.keys() if f in new_tree.keys() and self.current_tree[f]["folder"] and "gid" not in self.current_tree[f].keys()])
        new_files = set(
            filter(lambda x: "gid" not in x, new_files - new_folders))
        still_old = self.current_tree.keys() & new_tree.keys()

        renamed_files = []
        moved_files = []
        modified_files = []
        still_not_uploaded_files = []
        for k in still_old:
            if self.current_tree[k]["name"] != new_tree[k]["name"]:
                renamed_files.append(k)

            if self.current_tree[k]["pid"] != new_tree[k]["pid"] and not self.current_tree[k]["folder"]:
                moved_files.append(k)

            if "last_modified" in self.current_tree[k].keys() and k not in dowloading_files and \
               int(self.current_tree[k]["last_modified"]) != int(new_tree[k]["last_modified"]):
                modified_files.append(k)

            if "gid" not in self.current_tree[k]:
                still_not_uploaded_files.append(k)

        # copy gids if they exist
        for k, v in self.current_tree.items():
            if k in new_tree.keys() and "gid" in self.current_tree[k].keys():
                new_tree[k]["gid"] = self.current_tree[k]["gid"]

        # add the still downloading flag to the new tree elements
        for k in dowloading_files:
            if self.current_tree[k]["downloading"]:
                new_tree[k] = self.current_tree[k]

        return {
            "old_tree": self.current_tree,
            "current_tree": new_tree,
            "new_folders": new_folders,
            "new_files": new_files,
            "renamed_files": renamed_files,
            "moved_files": moved_files,
            "modified_files": modified_files,
            "deleted_files": deleted_files,
            "still_not_uploaded_files": still_not_uploaded_files
        }

    def load_last_tree(self):
        last_tree = {}
        if self.last_tree_id_file in os.listdir("."):
            with open(self.last_tree_id_file, "r") as last:
                last_tree = json.loads(last.read())

        return last_tree

    def _upload_folders(self, folder_doc, current_tree):
        folder_pid = folder_doc["pid"]

        if "gid" in folder_doc.keys():
            return folder_doc["gid"]

        # if the parent folder id is not in the
        # current tree it means it's in the root of the project
        # so we can upload it directly
        if folder_pid not in current_tree.keys() and "gid" not in folder_doc.keys():
            print("uploading folder %s to root" % folder_doc["name"])
            result = self.service.upload_folder(
                folder_doc["name"], self.base_folder_gid)
            return result["id"]

        if folder_pid in current_tree.keys() and "gid" not in current_tree[folder_pid].keys():
            gid = self._upload_folders(current_tree[folder_pid], current_tree)
            current_tree[folder_pid]["gid"] = gid

        print("uploading '%s' to '%s' with GID %s" % (
            folder_doc["name"], current_tree[folder_pid]["name"], current_tree[folder_pid]["gid"]))
        result = self.service.upload_folder(
            folder_doc["name"], current_tree[folder_pid]["gid"])

        return result["id"]

    def upload_folders(self, new_folder_ids, new_tree):
        for folder_id in new_folder_ids:
            print("trying upload for %s" % new_tree[folder_id]["name"])
            folder_doc = new_tree[folder_id]
            folder_doc["gid"] = self._upload_folders(folder_doc, new_tree)

            new_tree[folder_id] = folder_doc
        return new_tree

    def upload_files(self, new_file_ids, current_tree):
        for file_id in new_file_ids:
            self.uploader.submit(self.upload_file_job, file_id, current_tree)

        return current_tree

    def upload_file_job(self, file_id, current_tree):
        file_doc = current_tree[file_id]
        folder_doc = current_tree[file_doc["pid"]]
        print("uploading file %s to folder %s" %
              (file_doc["name"], folder_doc["name"]))
        try:
            progress_queue = Queue()

            def notify_progress():
                status = progress_queue.get()
                while status:
                    self.notification_queue.put(
                        FileUploadProgressNotification(
                            deepcopy(file_doc),
                            status["progress"],
                            status["in_failure"]),
                        False)
                    status = progress_queue.get()

            Thread(target=notify_progress).start()
            file_gid = file_doc["gid"] if "gid" in file_doc.keys() else None
            result = self.service.upload_file(file_id, file_doc["name"], file_doc["path"],
                                              file_gid, folder_doc["gid"], progress_queue=progress_queue)
            print("Upload of %s Complete!" % file_doc["name"])
        except Exception as e:
            print("unrecoverable error uploading file %s:" %
                  file_doc["name"], e)
            progress_queue.put(None)
            return

        if not result:
            print("upload job canceled for %s" % file_doc["name"])
            return

        self.notification_queue.put(
            FileCreatedNotification(deepcopy(file_doc)), False)
        self.add_gid(file_id, result["id"])

    def move_files(self, moved_files, old_tree, current_tree):
        for file_id in moved_files:
            old_doc = old_tree[file_id]
            new_doc = current_tree[file_id]
            print("moving file %s from %s to %s" % (old_doc["name"],
                                                    old_tree[old_doc["pid"]
                                                             ]["name"],
                                                    current_tree[new_doc["pid"]]["name"]))
            try:
                self.service.move_file(deepcopy(old_doc), deepcopy(new_doc),
                                       deepcopy(old_tree), deepcopy(current_tree))
                print("successfuly moved file %s from %s to %s" % (old_doc["name"],
                                                                   old_tree[old_doc["pid"]
                                                                            ]["name"],
                                                                   current_tree[new_doc["pid"]]["name"]))
                self.notification_queue.put(FileMovedNotification(deepcopy(new_doc),
                                                                  deepcopy(
                                                                      old_tree[old_doc["pid"]]),
                                                                  deepcopy(current_tree[new_doc["pid"]])), False)
            except Exception as e:
                print("error moving file:", old_doc)
                print(e)

    def cancel_upload(self, file_id):
        self.service.cancel(file_id)
