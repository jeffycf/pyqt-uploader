#!/usr/bin/env python
"""
A PyQt interface for uploading files to Shotgun.

Allows you to configure that app for a specific shotgun instance and directory structure.
Uploaded files need to be linked to an existing entity, but can have notes and tags applied.

Author: Rob Blau <rblau@laika.com>
"""

################################################################################
# DEFAULT PREFS: Change these if you want people to use the script without config changes
################################################################################
DEFAULT_SHOTGUN_API = "/net/a/place/where/we/put/the/shotgun_api3.py"
DEFAULT_SHOTGUN_URL = "http://shotgun.kickass-studios.com/"
DEFAULT_SHOTGUN_SCRIPT = "thumb_uploader"
DEFAULT_SHOTGUN_KEY = "ALONGSTRINGOFNUMBERSANDLETTERSFROMSHOTGUN"
DEFAULT_IMAGE_COMMAND = "convert $in $out"
DEFAULT_MOVIE_COMMAND = "ffmpeg -y -i $in -f mjpeg -ss $offset -vframes 1 -s svga -an $out"
DEFAULT_TAGS = "to_be_filed"
DEFAULT_PATH_FIELD = "sg_path_to_file"
DEFAULT_LINK_MAP = """\
Asset: /job_root/*/assets/$type/${name}
Task: /job_root/*/shots/$entity.Shot.name/${name}
"""

################################################################################
# THE ACTUAL CODE: You shouldn't need to go beyond this point for configuration
################################################################################
import re
import os
import sys
import urllib
import socket
import optparse
import tempfile
import mimetypes

from PyQt4 import QtGui
from PyQt4 import QtCore

try:
    from main_window import Ui_MainWindow
    from prefs_dialog import Ui_Preferences
except:
    # to work when catt'ed together for standalone emailing
    pass

################################################################################
# Globals
################################################################################
DEFAULT_COL_WIDTHS = "44,406,64,274,274"

################################################################################
# Model
################################################################################
class ShotgunFileModel(QtCore.QAbstractTableModel):
    # Mapping from display text to an attribute of the modeled object
    __HEADERS = [
        {'disp': 'Frame', 'attr': 'hero_offset', 'default': '', 'editable': True},
        {'disp': 'Path', 'attr': 'path', 'default': '', 'editable': False},
        {'disp': 'Linked To', 'attr': 'link_name', 'default': '', 'editable': False},
        {'disp': 'Tags', 'attr': 'tags', 'default': '', 'editable': True},
        {'disp': 'Note', 'attr': 'note', 'default': '', 'editable': True},
    ]

    # model methods
    def __init__(self, undo_stack, parent=None):
        """constructor"""
        super(ShotgunFileModel, self).__init__(parent)
        self.stack = undo_stack
        self.files = []

    def rowCount(self, parent=QtCore.QModelIndex()):
        """number of rows is the number of files"""
        return len(self.files)

    def columnCount(self, parent=QtCore.QModelIndex()):
        """number of columns is the number of headers"""
        return len(self.__HEADERS)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        """method to return data for the various display roles in qt"""
        # display role, actually return the info
        if index.isValid() and role == QtCore.Qt.DisplayRole:
            f = self.files[index.row()]
            attr = self.__HEADERS[index.column()]['attr']
            ret = getattr(f, attr, None)
            if ret is not None:
                return QtCore.QVariant(ret)
            else:
                return QtCore.QVariant(self.__HEADERS[index.column()]['default'])
        # not worrying about this role, just return invalid variant
        return QtCore.QVariant()

    def headerData(self, section, orientation, role):
        """return data for header row"""
        if orientation == QtCore.Qt.Horizontal and role == QtCore.Qt.DisplayRole:
            return QtCore.QVariant(self.__HEADERS[section]['disp'])
        return QtCore.QVariant()

    def setData(self, index, value, role):
        """set data for a given index"""
        if (role != QtCore.Qt.EditRole):
            return False
        self.stack.push(ChangeValueCommand(index, value, self))
        return True

    # drag and drop methods
    def supportedDropActions(self):
        """what drop actions do we support, treat move or copy as an insert"""
        return QtCore.Qt.MoveAction | QtCore.Qt.CopyAction

    def flags(self, index):
        """all items can accept drops, hero_offset is editable for video, otherwise use editable"""
        flags = QtCore.Qt.ItemIsDropEnabled | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
        if self.__HEADERS[index.column()]['attr'] == 'hero_offset':
            f = self.files[index.row()]
            mime_guess = mimetypes.guess_type(f.path)[0]
            if mime_guess is not None and mime_guess.startswith('video'):
                flags |= QtCore.Qt.ItemIsEditable
        elif self.__HEADERS[index.column()]['editable']:
            flags |= QtCore.Qt.ItemIsEditable
        return flags

    def mimeTypes(self):
        """return a list of supported mime types.  enables drop"""
        # text/uri-list is what most finder style file browsers use
        return QtCore.QStringList(['text/uri-list'])

    def dropMimeData(self, data, action, row, column, parent):
        """handle the drop action, currently just uri-list"""
        if data.hasFormat('text/uri-list'):
            if data.hasUrls():
                files = [str(url.toLocalFile()) for url in data.urls()]
            elif data.hasText():
                files = [str(urllib.urlretrieve(uri)[0]) for uri in str(data.text()).split() \
                                                        if urllib.splittype(uri)[0] == 'file']
            # Just let people know what's been added
            self.emit(QtCore.SIGNAL('filesAdded(QStringList)'), files)
            return True
        return False

    # model object access
    def _update_data(self, row, column, value):
        """update internal data at (row, column) to value"""
        f = self.files[row]
        setattr(f, self.__HEADERS[column]['attr'], str(value))
        index = self.index(row, column)
        self.emit(QtCore.SIGNAL('modelReset()'))
        # self.emit(QtCore.SIGNAL('dataChanged(index, index)'), index, index)

    def clear(self):
        self.files = []
        self.reset()
        self.emit(QtCore.SIGNAL('modelReset()'))

    def insert_files(self, files, row):
        self.beginInsertRows(QtCore.QModelIndex(), row, row+len(files)-1)
        self.files[row:row] = files
        self.endInsertRows()

    def append_files(self, files):
        self.insert_files(files, len(self.files))

    def delete_files(self, first, last):
        self.beginRemoveRows(QtCore.QModelIndex(), first, last)
        self.files[first:last] = []
        self.endRemoveRows()
        self.emit(QtCore.SIGNAL('modelReset()'))

################################################################################
# File Object
################################################################################
class ShotgunFile(object):
    """wrapper around info needed to upload a file"""
    def __init__(self, path, tags, link, hero_offset=None, note=''):
        self.path = path
        self.tags = tags
        self.hero_offset = ''
        self.link = link
        self.note = note
        self.link_name = link.get('name', link.get('code', ''))
        if hero_offset is None:
            # default offset to 1 for video mime-types
            mime_guess = mimetypes.guess_type(path)[0]
            if mime_guess is not None and mime_guess.startswith('video'):
                self.hero_offset = '1'
        self.size = os.path.getsize(path)

################################################################################
# Commands
################################################################################
class ChangeValueCommand(QtGui.QUndoCommand):
    def __init__(self, index, value, model, text='value change', parent=None):
        QtGui.QUndoCommand.__init__(self, text, parent)
        self.__old = index.data(QtCore.Qt.DisplayRole).toString()
        self.__new = value.toString()
        self.__row = index.row()
        self.__col = index.column()
        self.__model = model

    def redo(self):
        self.__model._update_data(self.__row, self.__col, self.__new)

    def undo(self):
        self.__model._update_data(self.__row, self.__col, self.__old)

################################################################################
class NewFileCommand(QtGui.QUndoCommand):
    def __init__(self, model, files, text='new files', parent=None):
        QtGui.QUndoCommand.__init__(self, text, parent)
        self.model = model
        self.files = files

    def redo(self):
        self.first = self.model.rowCount()
        self.model.append_files(self.files)
        self.last = self.model.rowCount()

    def undo(self):
        self.model.delete_files(self.first, self.last)

################################################################################
class DeleteFilesCommand(QtGui.QUndoCommand):
    def __init__(self, model, rows, text='delete files', parent=None):
        QtGui.QUndoCommand.__init__(self, text, parent)
        self.model = model
        self.files = dict([(row, model.files[row]) for row in rows])

    def redo(self):
        # make sure we do this in reverse order so rows stay accurate while
        # deleting
        for row in reversed(sorted(self.files.keys())):
            self.model.delete_files(row, row+1)

    def undo(self):
        # make sure we do this in row order to keep things as they were
        for row in sorted(self.files.keys()):
            self.model.insert_files([self.files[row]], row)

################################################################################
# Prefereneces
################################################################################
class PrefsDialog(QtGui.QDialog):
    def __init__(self):
        QtGui.QDialog.__init__(self)
        # setup gui
        self.gui = Ui_Preferences()
        self.gui.setupUi(self)
        # restore state
        settings = QtCore.QSettings('ShotgunSharing', 'uploader')
        self.gui.image_command.setText(settings.value("prefs/image_command", DEFAULT_IMAGE_COMMAND).toString())
        self.gui.movie_command.setText(settings.value("prefs/movie_command", DEFAULT_MOVIE_COMMAND).toString())
        self.gui.shotgun_url.setText(settings.value("prefs/shotgun_url", DEFAULT_SHOTGUN_URL).toString())
        self.gui.shotgun_script.setText(settings.value("prefs/shotgun_script", DEFAULT_SHOTGUN_SCRIPT).toString())
        self.gui.shotgun_key.setText(settings.value("prefs/shotgun_key", DEFAULT_SHOTGUN_KEY).toString())
        self.gui.shotgun_api.setText(settings.value("prefs/shotgun_api", DEFAULT_SHOTGUN_API).toString())
        self.gui.path_field.setText(settings.value("prefs/path_field", DEFAULT_PATH_FIELD).toString())
        self.gui.link_map.setText(settings.value("prefs/link_map", DEFAULT_LINK_MAP).toString())
        self.restoreGeometry(settings.value("prefs/geometry").toByteArray())
        # hook up buttons
        self.connect(self.gui.buttons, QtCore.SIGNAL('accepted()'), self.ok)
        self.connect(self.gui.buttons, QtCore.SIGNAL('rejected()'), self.cancel)
        self.__sync_with_fields()

    def __sync_with_fields(self):
        # outside classes should use these to access current settings
        self.image_command = str(self.gui.image_command.text())
        self.movie_command = str(self.gui.movie_command.text())
        self.shotgun_url = str(self.gui.shotgun_url.text())
        self.shotgun_script = str(self.gui.shotgun_script.text())
        self.shotgun_key = str(self.gui.shotgun_key.text())
        self.shotgun_api = str(self.gui.shotgun_api.text())
        self.path_field = str(self.gui.path_field.text())
        self.link_map = str(self.gui.link_map.toPlainText())

    def ok(self):
        # save settings
        settings = QtCore.QSettings('ShotgunSharing', 'uploader')
        settings.setValue("prefs/image_command", QtCore.QVariant(self.gui.image_command.text()))
        settings.setValue("prefs/movie_command", QtCore.QVariant(self.gui.movie_command.text()))
        settings.setValue("prefs/shotgun_url", QtCore.QVariant(self.gui.shotgun_url.text()))
        settings.setValue("prefs/shotgun_script", QtCore.QVariant(self.gui.shotgun_script.text()))
        settings.setValue("prefs/shotgun_key", QtCore.QVariant(self.gui.shotgun_key.text()))
        settings.setValue("prefs/shotgun_api", QtCore.QVariant(self.gui.shotgun_api.text()))
        settings.setValue("prefs/path_field", QtCore.QVariant(self.gui.path_field.text()))
        settings.setValue("prefs/link_map", QtCore.QVariant(self.gui.link_map.toPlainText()))
        settings.setValue("prefs/geometry", self.saveGeometry())
        # update instance members
        self.__sync_with_fields()

    def cancel(self):
        # always save geometry.  don't care about the other settings
        settings.setValue("prefs/geometry", self.saveGeometry())

################################################################################
# Main Window
################################################################################
class Uploader(QtGui.QMainWindow):
    def __init__(self):
        QtGui.QMainWindow.__init__(self)
        # setup gui
        self.gui = Ui_MainWindow()
        self.gui.setupUi(self)
        # setup undo/redo
        self.stack = QtGui.QUndoStack(self)
        undo = self.stack.createUndoAction(self)
        undo.setShortcut(QtGui.QKeySequence('Ctrl+Z'))
        redo = self.stack.createRedoAction(self)
        redo.setShortcut(QtGui.QKeySequence('Shift+Ctrl+Z'))
        self.gui.menuEdit.addAction(undo)
        self.gui.menuEdit.addAction(redo)
        # setup model
        self.model = ShotgunFileModel(self.stack, self.gui.file_table_view)
        self.gui.file_table_view.setModel(self.model)
        # connect up signals
        self.connect(self.gui.buttons, QtCore.SIGNAL('accepted()'), self.ok)
        self.connect(self.gui.buttons, QtCore.SIGNAL('rejected()'), self.close_window)
        self.connect(self.gui.action_Preferences, QtCore.SIGNAL('activated()'), self.do_prefs)
        self.connect(self.gui.action_Quit, QtCore.SIGNAL('activated()'), self.close_window)
        self.connect(self.gui.action_Add_Files, QtCore.SIGNAL('activated()'), self.add_files)
        self.connect(self.gui.action_Delete_Selected, QtCore.SIGNAL('activated()'), self.delete_selected)
        self.connect(self.gui.file_table_view.selectionModel(), QtCore.SIGNAL('selectionChanged(QItemSelection, QItemSelection)'), self.table_selection_changed)
        self.connect(self.model, QtCore.SIGNAL('filesAdded(QStringList)'), self.add_files)
        self.connect(self.gui.project, QtCore.SIGNAL('currentIndexChanged(QString)'), self.link_data_changed)
        self.connect(self.gui.link_type, QtCore.SIGNAL('currentIndexChanged(QString)'), self.link_data_changed)
        # default action states
        self.gui.action_Delete_Selected.setEnabled(False)
        # load up prefs
        self.prefs = PrefsDialog()
        # connect to shotgun
        self.__conn = None
        self.__conn = self.__connect_to_shotgun()
        if not self.__conn:
            self.gui.buttons.button(QtGui.QDialogButtonBox.Ok).setEnabled(False)
        else:
            self.gui.buttons.button(QtGui.QDialogButtonBox.Ok).setEnabled(True)
        # restore state
        settings = QtCore.QSettings('ShotgunSharing', 'uploader')
        self.gui.tags.setText(settings.value("main/tags", DEFAULT_TAGS).toString())
        self.restoreGeometry(settings.value("main/geometry").toByteArray())
        # restore column widths
        col_widths = [int(w.strip()) for w in str(settings.value("main/col_widths", DEFAULT_COL_WIDTHS).toString()).split(',') if w.strip()]
        for i in xrange(min(len(col_widths), self.model.columnCount())):
            self.gui.file_table_view.setColumnWidth(i, col_widths[i])

    def __connect_to_shotgun(self):
        """
        Try to connect to shotgun, alerting on failure.
        If connected, sync up interface with shotgun instance settings.
        """
        # try to load up the shotgun api
        try:
            if os.path.isdir(self.prefs.shotgun_api):
                sys.path.append(self.prefs.shotgun_api)
            else:
                sys.path.append(os.path.dirname(self.prefs.shotgun_api))
            import shotgun_api3_preview as sg
        except ImportError:
            sys.path.pop()
            QtGui.QMessageBox.critical(self, self.tr("uploader"),
                self.tr("shotgun_api3_preview module not found.  Update your Preferences."),
                QtGui.QMessageBox.Ok)
            return None
        conn = sg.Shotgun(self.prefs.shotgun_url, self.prefs.shotgun_script, self.prefs.shotgun_key)
        # validate connection by seeing if Attachments are accessible
        try:
            # use path_field to validate that is set right, if it is set
            conn.schema_field_read('Attachment', self.prefs.path_field or 'project')
        except socket.gaierror:
            # raised if connection straight out failed
            QtGui.QMessageBox.critical(self, self.tr("uploader"),
                self.tr("cannot connect to shotgun host.  Update your Preferences."),
                QtGui.QMessageBox.Ok)
            return None
        except sg.Fault, e:
            if e.faultCode == 102:
                # raised if authentication prefs aren't right
                QtGui.QMessageBox.critical(self, self.tr("uploader"),
                    self.tr("cannot authenticate shotgun script.  Update your Preferences."),
                    QtGui.QMessageBox.Ok)
                return None
            if e.faultCode == 103:
                # raised if Attachments aren't available
                if 'Valid entity types' in e.faultString:
                    QtGui.QMessageBox.critical(self, self.tr("uploader"),
                        self.tr("cannot work with Files through the API.  Ask the shotgun guys to turn that on for you."),
                        QtGui.QMessageBox.Ok)
                    # exit on this one... no pref changes are going to help
                    sys.exit(1)
                # otherwise it is a field lookup failuer on path_field
                QtGui.QMessageBox.warning(self, self.tr("uploader"),
                    self.tr("Attachment has no field '%s'.  Update your Preferences." % self.prefs.path_field),
                    QtGui.QMessageBox.Ok)
        if conn:
            # got a conn.  get sync'ed up with it
            self.__conn = conn
            # load up defaults for entity selection fields
            settings = QtCore.QSettings('ShotgunSharing', 'uploader')
            default_project = str(settings.value("main/project", '').toString())
            default_link_type = str(settings.value("main/link_type", '').toString())
            # load up projects, populate the completer and combo box
            projects = [(p['name'], p['id']) for p in conn.find('Project', [], ['name']) if p['name'] != 'Template Project']
            completer = QtGui.QCompleter([p[0] for p in projects], self)
            completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
            self.gui.project.clear()
            for p in projects:
                self.gui.project.addItem(p[0], p[1])
                if p[0] == default_project:
                    # name matched default project, select it
                    self.gui.project.setCurrentIndex(self.gui.project.count()-1)
            self.gui.project.setCompleter(completer)
            self.gui.project.setValidator(QtGui.QRegExpValidator(QtCore.QRegExp('|'.join([p[0] for p in projects]), QtCore.Qt.CaseInsensitive), self))
            # setup link type, populate the completer and combo box
            # TODO: should make sure these are active in the instance
            link_types = ['', 'Asset', 'Scene', 'Sequence', 'Shot', 'Task', 'Project', 'Tool', 'Ticket']
            completer = QtGui.QCompleter(link_types, self)
            completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
            self.gui.link_type.clear()
            for l in link_types:
                self.gui.link_type.addItem(l)
                if l == default_link_type:
                    # name matches default, update selection
                    self.gui.link_type.setCurrentIndex(self.gui.link_type.count()-1)
            self.gui.link_type.setCompleter(completer)
            self.gui.link_type.setValidator(QtGui.QRegExpValidator(QtCore.QRegExp('|'.join(link_types), QtCore.Qt.CaseInsensitive), self))
            # default used to update reference to
            self.default_link = conn.find_one('HumanUser', [['login', 'is', os.getlogin()]], ['name'])
            return conn
        return None

    def link_data_changed(self, ignored):
        """respond to an update for project or link type"""
        # zero out the selected link
        self.gui.link_name.clear()
        project = int(self.gui.project.itemData(self.gui.project.currentIndex()).toInt()[0])
        link_type = str(self.gui.link_type.currentText())
        if not link_type:
            # can't do anything without link type selected
            return
        filters = project and [['project', 'is', {'type': 'Project', 'id': project}]] or []
        if link_type in ['Project']:
            filters = []
        # find possible matches, grabbing values that are useful for generating
        # a pretty name for the different entity types
        matches = self.__conn.find(link_type, filters, ['display_name', 'content', 'name', 'code', 'sg_sequence', 'sg_asset_type'])
        texts = ['']
        self.gui.link_name.addItem('', 0)
        if matches and matches[0].has_key('sg_sequence'):
            # going to need sequences
            seqs = self.__conn.find('Sequence', filters, ['code'])
            seq_map = dict([(s['id'], s['code']) for s in seqs])
        for match in matches:
            # TODO: make the set of extra info included with a name richer
            # look for something that has a good name
            text = match.get('display_name', match.get('content', match.get('name', match.get('code', None))))
            if text is None:
                # just in case the entity type has no field we know about as a
                # useful name
                break
            if match.has_key('sg_sequence'):
                # display the sequence if we've got it
                text = text + " (%s)" % (match['sg_sequence'] and seq_map[match['sg_sequence']['id']] or 'None')
            elif match.has_key('sg_asset_type'):
                # display the asset type if we've got it
                text = text + " (%s)" % match['sg_asset_type']
            self.gui.link_name.addItem(text, match['id'])
            texts.append(text)
        # And setup the completer
        completer = QtGui.QCompleter(texts, self)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.gui.link_name.setCompleter(completer)
        self.gui.link_name.setValidator(QtGui.QRegExpValidator(QtCore.QRegExp('|'.join(texts), QtCore.Qt.CaseInsensitive), self))

    def delete_selected(self):
        """grab selection and delete it"""
        rows = dict([(index.row(), None) for index in self.gui.file_table_view.selectedIndexes()]).keys()
        self.stack.push(DeleteFilesCommand(self.model, rows))

    def add_files(self, fnames=None):
        if fnames is None:
            # pop up the file chooser dialog box if the files weren't passed in
            settings = QtCore.QSettings('ShotgunSharing', 'uploader')
            # default to the last dir we used
            d = settings.value("fdialog/dir", QtCore.QString()).toString()
            fnames = [str(f) for f in QtGui.QFileDialog.getOpenFileNames(self, 'Select files to upload', d)]
            if fnames:
                # save the dir of the first file selected
                settings.setValue("fdialog/dir", os.path.dirname(fnames[0]))
        if fnames:
            tags = str(self.gui.tags.text())
            files = []
            project = int(self.gui.project.itemData(self.gui.project.currentIndex()).toInt()[0])
            project_text = str(self.gui.project.currentText())
            skipped_fnames = []
            for fname in [str(f) for f in fnames]:
                if str(self.gui.link_name.currentText()):
                    # if link_name has been selected, use all the info used to
                    # get to that entity
                    link_type = str(self.gui.link_type.currentText())
                    link_name = str(self.gui.link_name.currentText())
                    link_id = int(self.gui.link_name.itemData(self.gui.link_name.currentIndex()).toInt()[0])
                    link = {'type': link_type, 'name': link_name, 'id': link_id, 'project': {'type': 'Project', 'id': project}}
                else:
                    # otherwise try to figure out the link from the path rules
                    # in the prefs
                    link = self.__link_for_file(fname)
                if link is None:
                    # didn't have a link, remember that so we can error
                    skipped_fnames.append(fname)
                    continue
                f = ShotgunFile(str(fname), tags, link)
                files.append(f)
            # we've got the file objects we're going to create.  do it
            self.stack.push(NewFileCommand(self.model, files))
        for i in xrange(self.model.columnCount()):
            # make sure we can see the data
            self.gui.file_table_view.resizeColumnToContents(i)
        if skipped_fnames:
            # and let people know if we skipped something becuase of no links
            QtGui.QMessageBox.warning(self, self.tr("uploader"),
                self.tr("Couldn't figure out link for:\n%s\n\nPlease select what to link it to." % '\n'.join(skipped_fnames)),
                QtGui.QMessageBox.Ok)

    # Pattern - find an odd # of $ which doesn't have a $ before it
    #   then match {pattern} or pattern, keep pattern in the match group 'name'
    __PATTERN_RE = re.compile(r'(?<!\$)\$(\$\$)*{?(?P<name>[_a-z][_a-z0-9.]*)}?')
    def __link_for_file(self, fname, warn=False):
        try:
            import shotgun_api3_preview as sg
            for line in [line for line in self.prefs.link_map.split('\n') if line]:
                try:
                    # translate the lines link_type: match into a useful regexp
                    (link_type, link_match) = [s.strip() for s in line.split(':', 1)]
                    re_match = link_match
                    # translate glob syntax to regexp
                    re_match = re_match.replace('?', '.{1}')
                    re_match = re_match.replace('*', '[^%s]+' % os.sep)
                    name_map = {}
                    def track_names(match):
                        """map temp group names to the pattern they are generated for"""
                        pat = '(?P<name%s>[^%s]+)' % (len(name_map), os.sep)
                        name_map['name%d' % len(name_map)] = match.group('name')
                        return pat
                    # find all variable replacements and replace them with regexp
                    # syntax that'll grab the text from the file paths for us
                    result_re = self.__PATTERN_RE.sub(track_names, re_match)
                    # now we've got a workable regexp, match against the file name
                    match = re.search(result_re, fname)
                    if match:
                        # we have a rule that matches, create a shotgun filter
                        # representing that match and see if it worked
                        filters = []
                        for (key, value) in match.groupdict().iteritems():
                            filters.append([name_map[key], 'is', value])
                        link = self.__conn.find_one(link_type, filters, ['name', 'code'])
                        return link
                except ValueError:
                    if warn:
                        QtGui.QMessageBox.warning(self, self.tr("uploader"),
                            self.tr("Couldn't parse link map line '%s'.  Fix your Preferences." % line),
                            QtGui.QMessageBox.Ok)
                except sg.Fault:
                    # error in shotgun, no link, keep on trying
                    pass
            # no matches
        except ImportError:
            # couldn't import shotgun, not configured correctly, just return
            pass
        return None

    def table_selection_changed(self, selected=None, deselected=None):
        """allow delete selected only when there is a row selected"""
        columns = self.model.columnCount()
        rows = len(self.gui.file_table_view.selectedIndexes())/columns
        any = (rows > 0)
        self.gui.action_Delete_Selected.setEnabled(any)

    def do_prefs(self):
        """show the prefs dialog and resync with shotgun"""
        if self.prefs.exec_() == QtGui.QDialog.Accepted:
            self.__conn = self.__connect_to_shotgun()
            self.__link_for_file('', warn=True)
        if not self.__conn:
            self.gui.buttons.button(QtGui.QDialogButtonBox.Ok).setEnabled(False)
        else:
            self.gui.buttons.button(QtGui.QDialogButtonBox.Ok).setEnabled(True)

    def ok(self):
        """make the magic happen"""
        conn = self.__conn
        prog = QtGui.QProgressDialog()
        # guess that progress will progress along with bytes uploaded
        maximum = sum([f.size for f in self.model.files])
        nfiles = len (self.model.files)
        prog.setMaximum(maximum)
        prog.setLabelText("%-80s" % "Uploading %d/%d: %s" % (0, nfiles, ''))
        prog.setValue(0)
        prog.show()
        i = 0
        while self.model.files:
            i += 1
            f = self.model.files[0]
            # let folks know what we're doing
            prog.setLabelText("%-80s" % "Uploading %d/%d: %s" % (i, nfiles, f.path))
            if prog.wasCanceled():
                break
            # upload
            f_id = conn.upload(f.link['type'], f.link['id'], f.path)
            # update tags, path, and reference for
            data = {'attachment_reference_links': [self.default_link]}
            if self.prefs.path_field:
                data[self.prefs.path_field] = f.path
            if f.tags:
                data['tag_list'] = f.tags.split(',')
            conn.update('Attachment', f_id, data)
            # do thumbnails for files we can
            mime = mimetypes.guess_type(f.path)[0]
            if mime is not None and (mime.startswith('video') or mime.startswith('image')):
                tmp = tempfile.mktemp('.jpg', 'uploader_')
                try:
                    # figure out which command to run
                    cmd = None
                    if mime.startswith('image'):
                        cmd = self.prefs.image_command
                    elif mime.startswith('video'):
                        cmd = self.prefs.movie_command
                    if cmd is not None:
                        # do the replacements for the command and run it
                        cmd = cmd.replace('$in', '"%s"'% f.path)
                        cmd = cmd.replace('$out', '"%s"' % tmp)
                        cmd = cmd.replace('$offset', '"%s"' % f.hero_offset)
                        os.system(cmd)
                        if os.path.exists(tmp):
                            # it worked, upload the thumbnail
                            conn.upload_thumbnail('Attachment', f_id, tmp)
                finally:
                    # make sure we clean up
                    if os.path.exists(tmp):
                        os.remove(tmp)
            if f.note:
                # add the note if set
                conn.create('Note', {'content': f.note, 'note_links': [{'type': 'Attachment', 'id': f_id}], \
                    'project': f.link.get('project', f.link)})
            # update progress
            prog.setValue(prog.value()+f.size)
            # get rid of the file from the interface
            self.model.delete_files(0, 1)
            # allow the gui to update
            QtGui.QApplication.processEvents()
        # all done, make sure we're clear
        prog.setValue(maximum)
        self.stack.clear()

    def close_window(self):
        # save state
        settings = QtCore.QSettings('ShotgunSharing', 'uploader')
        settings.setValue("main/tags", QtCore.QVariant(self.gui.tags.text()))
        settings.setValue("main/project", QtCore.QVariant(self.gui.project.currentText()))
        settings.setValue("main/link_type", QtCore.QVariant(self.gui.link_type.currentText()))
        settings.setValue("main/geometry", self.saveGeometry())
        col_widths = ','.join([str(self.gui.file_table_view.columnWidth(i)) \
                                for i in xrange(self.model.columnCount())])
        settings.setValue("main/col_widths", QtCore.QVariant(col_widths))
        # shut down
        self.close()

# -*- coding: utf-8 -*-

# Form implementation generated from reading ui file 'main_window.ui'
#
# Created: Tue Mar 16 22:32:04 2010
#      by: PyQt4 UI code generator 4.6.2
#
# WARNING! All changes made in this file will be lost!

from PyQt4 import QtCore, QtGui

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        MainWindow.resize(617, 471)
        self.centralwidget = QtGui.QWidget(MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        self.verticalLayout = QtGui.QVBoxLayout(self.centralwidget)
        self.verticalLayout.setObjectName("verticalLayout")
        self.label_3 = QtGui.QLabel(self.centralwidget)
        self.label_3.setTextFormat(QtCore.Qt.AutoText)
        self.label_3.setWordWrap(True)
        self.label_3.setObjectName("label_3")
        self.verticalLayout.addWidget(self.label_3)
        self.groupBox = QtGui.QGroupBox(self.centralwidget)
        self.groupBox.setObjectName("groupBox")
        self.horizontalLayout_3 = QtGui.QHBoxLayout(self.groupBox)
        self.horizontalLayout_3.setObjectName("horizontalLayout_3")
        self.frame = QtGui.QFrame(self.groupBox)
        self.frame.setFrameShape(QtGui.QFrame.NoFrame)
        self.frame.setFrameShadow(QtGui.QFrame.Raised)
        self.frame.setObjectName("frame")
        self.gridLayout = QtGui.QGridLayout(self.frame)
        self.gridLayout.setMargin(0)
        self.gridLayout.setObjectName("gridLayout")
        self.label = QtGui.QLabel(self.frame)
        self.label.setMaximumSize(QtCore.QSize(47, 20))
        self.label.setObjectName("label")
        self.gridLayout.addWidget(self.label, 0, 0, 1, 1)
        self.project = QtGui.QComboBox(self.frame)
        self.project.setEditable(True)
        self.project.setInsertPolicy(QtGui.QComboBox.NoInsert)
        self.project.setFrame(True)
        self.project.setObjectName("project")
        self.gridLayout.addWidget(self.project, 0, 1, 1, 1)
        self.label_4 = QtGui.QLabel(self.frame)
        self.label_4.setObjectName("label_4")
        self.gridLayout.addWidget(self.label_4, 0, 2, 1, 1)
        self.tags = QtGui.QLineEdit(self.frame)
        self.tags.setObjectName("tags")
        self.gridLayout.addWidget(self.tags, 0, 3, 1, 1)
        self.label_2 = QtGui.QLabel(self.frame)
        self.label_2.setMaximumSize(QtCore.QSize(65, 20))
        self.label_2.setObjectName("label_2")
        self.gridLayout.addWidget(self.label_2, 1, 0, 1, 1)
        self.link_type = QtGui.QComboBox(self.frame)
        self.link_type.setEditable(True)
        self.link_type.setInsertPolicy(QtGui.QComboBox.NoInsert)
        self.link_type.setObjectName("link_type")
        self.gridLayout.addWidget(self.link_type, 1, 1, 1, 1)
        self.label_5 = QtGui.QLabel(self.frame)
        self.label_5.setMaximumSize(QtCore.QSize(71, 20))
        self.label_5.setObjectName("label_5")
        self.gridLayout.addWidget(self.label_5, 1, 2, 1, 1)
        self.link_name = QtGui.QComboBox(self.frame)
        sizePolicy = QtGui.QSizePolicy(QtGui.QSizePolicy.Preferred, QtGui.QSizePolicy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.link_name.sizePolicy().hasHeightForWidth())
        self.link_name.setSizePolicy(sizePolicy)
        self.link_name.setEditable(True)
        self.link_name.setInsertPolicy(QtGui.QComboBox.NoInsert)
        self.link_name.setSizeAdjustPolicy(QtGui.QComboBox.AdjustToContentsOnFirstShow)
        self.link_name.setObjectName("link_name")
        self.gridLayout.addWidget(self.link_name, 1, 3, 1, 1)
        self.horizontalLayout_3.addWidget(self.frame)
        self.verticalLayout.addWidget(self.groupBox)
        self.groupBox_2 = QtGui.QGroupBox(self.centralwidget)
        self.groupBox_2.setObjectName("groupBox_2")
        self.horizontalLayout_4 = QtGui.QHBoxLayout(self.groupBox_2)
        self.horizontalLayout_4.setObjectName("horizontalLayout_4")
        self.file_table_view = QtGui.QTableView(self.groupBox_2)
        font = QtGui.QFont()
        font.setFamily("Helvetica")
        font.setPointSize(12)
        self.file_table_view.setFont(font)
        self.file_table_view.setEditTriggers(QtGui.QAbstractItemView.AnyKeyPressed|QtGui.QAbstractItemView.DoubleClicked|QtGui.QAbstractItemView.EditKeyPressed|QtGui.QAbstractItemView.SelectedClicked)
        self.file_table_view.setDragDropMode(QtGui.QAbstractItemView.DropOnly)
        self.file_table_view.setAlternatingRowColors(True)
        self.file_table_view.setSelectionBehavior(QtGui.QAbstractItemView.SelectRows)
        self.file_table_view.setSortingEnabled(False)
        self.file_table_view.setWordWrap(False)
        self.file_table_view.setObjectName("file_table_view")
        self.file_table_view.horizontalHeader().setDefaultSectionSize(40)
        self.file_table_view.horizontalHeader().setHighlightSections(True)
        self.file_table_view.horizontalHeader().setStretchLastSection(True)
        self.file_table_view.verticalHeader().setVisible(False)
        self.file_table_view.verticalHeader().setStretchLastSection(False)
        self.horizontalLayout_4.addWidget(self.file_table_view)
        self.verticalLayout.addWidget(self.groupBox_2)
        self.frame_3 = QtGui.QFrame(self.centralwidget)
        self.frame_3.setFrameShape(QtGui.QFrame.NoFrame)
        self.frame_3.setFrameShadow(QtGui.QFrame.Raised)
        self.frame_3.setObjectName("frame_3")
        self.horizontalLayout = QtGui.QHBoxLayout(self.frame_3)
        self.horizontalLayout.setMargin(0)
        self.horizontalLayout.setObjectName("horizontalLayout")
        self.buttons = QtGui.QDialogButtonBox(self.frame_3)
        self.buttons.setOrientation(QtCore.Qt.Horizontal)
        self.buttons.setStandardButtons(QtGui.QDialogButtonBox.Close|QtGui.QDialogButtonBox.Ok)
        self.buttons.setObjectName("buttons")
        self.horizontalLayout.addWidget(self.buttons)
        self.verticalLayout.addWidget(self.frame_3)
        MainWindow.setCentralWidget(self.centralwidget)
        self.menubar = QtGui.QMenuBar(MainWindow)
        self.menubar.setGeometry(QtCore.QRect(0, 0, 617, 22))
        self.menubar.setObjectName("menubar")
        self.menu_File = QtGui.QMenu(self.menubar)
        self.menu_File.setObjectName("menu_File")
        self.menuEdit = QtGui.QMenu(self.menubar)
        self.menuEdit.setObjectName("menuEdit")
        MainWindow.setMenuBar(self.menubar)
        self.statusbar = QtGui.QStatusBar(MainWindow)
        self.statusbar.setObjectName("statusbar")
        MainWindow.setStatusBar(self.statusbar)
        self.action_Preferences = QtGui.QAction(MainWindow)
        self.action_Preferences.setObjectName("action_Preferences")
        self.action_Quit = QtGui.QAction(MainWindow)
        self.action_Quit.setObjectName("action_Quit")
        self.action_Add_Files = QtGui.QAction(MainWindow)
        self.action_Add_Files.setObjectName("action_Add_Files")
        self.action_Delete_Selected = QtGui.QAction(MainWindow)
        self.action_Delete_Selected.setObjectName("action_Delete_Selected")
        self.menu_File.addAction(self.action_Add_Files)
        self.menu_File.addAction(self.action_Delete_Selected)
        self.menu_File.addSeparator()
        self.menu_File.addAction(self.action_Preferences)
        self.menu_File.addAction(self.action_Quit)
        self.menubar.addAction(self.menu_File.menuAction())
        self.menubar.addAction(self.menuEdit.menuAction())
        self.label_3.setBuddy(self.file_table_view)
        self.label_4.setBuddy(self.tags)

        self.retranslateUi(MainWindow)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

    def retranslateUi(self, MainWindow):
        MainWindow.setWindowTitle(QtGui.QApplication.translate("MainWindow", "Shotgun File Uploader", None, QtGui.QApplication.UnicodeUTF8))
        self.label_3.setText(QtGui.QApplication.translate("MainWindow", "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.0//EN\" \"http://www.w3.org/TR/REC-html40/strict.dtd\">\n"
"<html><head><meta name=\"qrichtext\" content=\"1\" /><style type=\"text/css\">\n"
"p, li { white-space: pre-wrap; }\n"
"</style></head><body style=\" font-family:\'Lucida Grande\'; font-size:13pt; font-weight:400; font-style:normal;\">\n"
"<p style=\" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;\"><span style=\" font-size:9pt;\">Instructions: </span><span style=\" font-size:9pt; font-weight:600;\">Add files via the menu or drag &amp; drop</span><span style=\" font-size:9pt;\">.  Just type to </span><span style=\" font-size:9pt; font-weight:600;\">adjust the hero frames for movies</span><span style=\" font-size:9pt;\">.  Hint: You can use a tag like \"to_be_filed\" to be able to easily find uploaded Files in Shotgun.  Select Project/Link Type/Link Name to </span><span style=\" font-size:9pt; font-weight:600;\">link added files to a specific entity</span><span style=\" font-size:9pt;\">.  Leave these blank to </span><span style=\" font-size:9pt; font-weight:600;\">let the config try to figure out the linked entity from the file path</span><span style=\" font-size:9pt;\">.  If all else fails, the file will be linked to your shotgun account for easy searching.  Configure Shotgun access and thumbnail generation through the preferences.</span></p></body></html>", None, QtGui.QApplication.UnicodeUTF8))
        self.groupBox.setTitle(QtGui.QApplication.translate("MainWindow", "Shotgun Defaults", None, QtGui.QApplication.UnicodeUTF8))
        self.label.setText(QtGui.QApplication.translate("MainWindow", "Project:", None, QtGui.QApplication.UnicodeUTF8))
        self.label_4.setText(QtGui.QApplication.translate("MainWindow", "Tags:", None, QtGui.QApplication.UnicodeUTF8))
        self.tags.setText(QtGui.QApplication.translate("MainWindow", "to_be_filed", None, QtGui.QApplication.UnicodeUTF8))
        self.label_2.setText(QtGui.QApplication.translate("MainWindow", "Link Type:", None, QtGui.QApplication.UnicodeUTF8))
        self.label_5.setText(QtGui.QApplication.translate("MainWindow", "Link Name:", None, QtGui.QApplication.UnicodeUTF8))
        self.groupBox_2.setTitle(QtGui.QApplication.translate("MainWindow", "Files to Upload", None, QtGui.QApplication.UnicodeUTF8))
        self.menu_File.setTitle(QtGui.QApplication.translate("MainWindow", "&File", None, QtGui.QApplication.UnicodeUTF8))
        self.menuEdit.setTitle(QtGui.QApplication.translate("MainWindow", "Edit", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Preferences.setText(QtGui.QApplication.translate("MainWindow", "Preferences...", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Preferences.setShortcut(QtGui.QApplication.translate("MainWindow", "Ctrl+,", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Quit.setText(QtGui.QApplication.translate("MainWindow", "&Quit", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Quit.setShortcut(QtGui.QApplication.translate("MainWindow", "Ctrl+Q", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Add_Files.setText(QtGui.QApplication.translate("MainWindow", "&Add Files...", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Add_Files.setShortcut(QtGui.QApplication.translate("MainWindow", "Ctrl+O", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Delete_Selected.setText(QtGui.QApplication.translate("MainWindow", "&Delete Selected", None, QtGui.QApplication.UnicodeUTF8))
        self.action_Delete_Selected.setShortcut(QtGui.QApplication.translate("MainWindow", "Ctrl+Backspace", None, QtGui.QApplication.UnicodeUTF8))

# -*- coding: utf-8 -*-

# Form implementation generated from reading ui file 'prefs_dialog.ui'
#
# Created: Tue Mar 16 16:40:24 2010
#      by: PyQt4 UI code generator 4.6.2
#
# WARNING! All changes made in this file will be lost!

from PyQt4 import QtCore, QtGui

class Ui_Preferences(object):
    def setupUi(self, Preferences):
        Preferences.setObjectName("Preferences")
        Preferences.resize(662, 722)
        Preferences.setMinimumSize(QtCore.QSize(629, 419))
        self.gridLayout = QtGui.QGridLayout(Preferences)
        self.gridLayout.setObjectName("gridLayout")
        self.groupBox = QtGui.QGroupBox(Preferences)
        self.groupBox.setObjectName("groupBox")
        self.verticalLayout = QtGui.QVBoxLayout(self.groupBox)
        self.verticalLayout.setSpacing(0)
        self.verticalLayout.setMargin(0)
        self.verticalLayout.setObjectName("verticalLayout")
        self.frame_2 = QtGui.QFrame(self.groupBox)
        self.frame_2.setFrameShape(QtGui.QFrame.NoFrame)
        self.frame_2.setFrameShadow(QtGui.QFrame.Raised)
        self.frame_2.setObjectName("frame_2")
        self.gridLayout_4 = QtGui.QGridLayout(self.frame_2)
        self.gridLayout_4.setObjectName("gridLayout_4")
        self.label_2 = QtGui.QLabel(self.frame_2)
        font = QtGui.QFont()
        font.setWeight(75)
        font.setBold(True)
        self.label_2.setFont(font)
        self.label_2.setObjectName("label_2")
        self.gridLayout_4.addWidget(self.label_2, 1, 0, 1, 1)
        self.label = QtGui.QLabel(self.frame_2)
        font = QtGui.QFont()
        font.setWeight(75)
        font.setBold(True)
        self.label.setFont(font)
        self.label.setObjectName("label")
        self.gridLayout_4.addWidget(self.label, 0, 0, 1, 1)
        self.movie_command = QtGui.QLineEdit(self.frame_2)
        self.movie_command.setObjectName("movie_command")
        self.gridLayout_4.addWidget(self.movie_command, 1, 1, 1, 1)
        self.image_command = QtGui.QLineEdit(self.frame_2)
        self.image_command.setObjectName("image_command")
        self.gridLayout_4.addWidget(self.image_command, 0, 1, 1, 1)
        self.verticalLayout.addWidget(self.frame_2)
        self.frame = QtGui.QFrame(self.groupBox)
        self.frame.setFrameShape(QtGui.QFrame.NoFrame)
        self.frame.setFrameShadow(QtGui.QFrame.Raised)
        self.frame.setObjectName("frame")
        self.horizontalLayout = QtGui.QHBoxLayout(self.frame)
        self.horizontalLayout.setObjectName("horizontalLayout")
        self.label_8 = QtGui.QLabel(self.frame)
        self.label_8.setWordWrap(True)
        self.label_8.setObjectName("label_8")
        self.horizontalLayout.addWidget(self.label_8)
        self.verticalLayout.addWidget(self.frame)
        self.gridLayout.addWidget(self.groupBox, 0, 0, 1, 1)
        self.buttons = QtGui.QDialogButtonBox(Preferences)
        self.buttons.setOrientation(QtCore.Qt.Horizontal)
        self.buttons.setStandardButtons(QtGui.QDialogButtonBox.Cancel|QtGui.QDialogButtonBox.Ok)
        self.buttons.setObjectName("buttons")
        self.gridLayout.addWidget(self.buttons, 3, 0, 1, 1)
        self.groupBox_2 = QtGui.QGroupBox(Preferences)
        self.groupBox_2.setObjectName("groupBox_2")
        self.gridLayout_3 = QtGui.QGridLayout(self.groupBox_2)
        self.gridLayout_3.setObjectName("gridLayout_3")
        self.frame_3 = QtGui.QFrame(self.groupBox_2)
        self.frame_3.setFrameShape(QtGui.QFrame.NoFrame)
        self.frame_3.setFrameShadow(QtGui.QFrame.Raised)
        self.frame_3.setObjectName("frame_3")
        self.gridLayout_2 = QtGui.QGridLayout(self.frame_3)
        self.gridLayout_2.setMargin(0)
        self.gridLayout_2.setObjectName("gridLayout_2")
        self.label_3 = QtGui.QLabel(self.frame_3)
        font = QtGui.QFont()
        font.setWeight(75)
        font.setBold(True)
        self.label_3.setFont(font)
        self.label_3.setObjectName("label_3")
        self.gridLayout_2.addWidget(self.label_3, 0, 0, 1, 1)
        self.shotgun_url = QtGui.QLineEdit(self.frame_3)
        self.shotgun_url.setObjectName("shotgun_url")
        self.gridLayout_2.addWidget(self.shotgun_url, 0, 1, 1, 1)
        self.label_4 = QtGui.QLabel(self.frame_3)
        font = QtGui.QFont()
        font.setWeight(75)
        font.setBold(True)
        self.label_4.setFont(font)
        self.label_4.setObjectName("label_4")
        self.gridLayout_2.addWidget(self.label_4, 1, 0, 1, 1)
        self.label_5 = QtGui.QLabel(self.frame_3)
        font = QtGui.QFont()
        font.setWeight(75)
        font.setBold(True)
        self.label_5.setFont(font)
        self.label_5.setObjectName("label_5")
        self.gridLayout_2.addWidget(self.label_5, 2, 0, 1, 1)
        self.label_6 = QtGui.QLabel(self.frame_3)
        font = QtGui.QFont()
        font.setWeight(75)
        font.setBold(True)
        self.label_6.setFont(font)
        self.label_6.setObjectName("label_6")
        self.gridLayout_2.addWidget(self.label_6, 3, 0, 1, 1)
        self.label_7 = QtGui.QLabel(self.frame_3)
        self.label_7.setObjectName("label_7")
        self.gridLayout_2.addWidget(self.label_7, 4, 0, 1, 1)
        self.shotgun_script = QtGui.QLineEdit(self.frame_3)
        self.shotgun_script.setObjectName("shotgun_script")
        self.gridLayout_2.addWidget(self.shotgun_script, 1, 1, 1, 1)
        self.shotgun_key = QtGui.QLineEdit(self.frame_3)
        self.shotgun_key.setObjectName("shotgun_key")
        self.gridLayout_2.addWidget(self.shotgun_key, 2, 1, 1, 1)
        self.shotgun_api = QtGui.QLineEdit(self.frame_3)
        self.shotgun_api.setObjectName("shotgun_api")
        self.gridLayout_2.addWidget(self.shotgun_api, 3, 1, 1, 1)
        self.path_field = QtGui.QLineEdit(self.frame_3)
        self.path_field.setObjectName("path_field")
        self.gridLayout_2.addWidget(self.path_field, 4, 1, 1, 1)
        self.gridLayout_3.addWidget(self.frame_3, 5, 1, 1, 1)
        self.label_9 = QtGui.QLabel(self.groupBox_2)
        self.label_9.setWordWrap(True)
        self.label_9.setObjectName("label_9")
        self.gridLayout_3.addWidget(self.label_9, 6, 1, 1, 1)
        self.gridLayout.addWidget(self.groupBox_2, 1, 0, 1, 1)
        self.groupBox_3 = QtGui.QGroupBox(Preferences)
        self.groupBox_3.setObjectName("groupBox_3")
        self.verticalLayout_2 = QtGui.QVBoxLayout(self.groupBox_3)
        self.verticalLayout_2.setObjectName("verticalLayout_2")
        self.link_map = QtGui.QTextEdit(self.groupBox_3)
        self.link_map.setTabChangesFocus(True)
        self.link_map.setAcceptRichText(False)
        self.link_map.setObjectName("link_map")
        self.verticalLayout_2.addWidget(self.link_map)
        self.label_10 = QtGui.QLabel(self.groupBox_3)
        self.label_10.setWordWrap(True)
        self.label_10.setObjectName("label_10")
        self.verticalLayout_2.addWidget(self.label_10)
        self.gridLayout.addWidget(self.groupBox_3, 2, 0, 1, 1)

        self.retranslateUi(Preferences)
        QtCore.QObject.connect(self.buttons, QtCore.SIGNAL("accepted()"), Preferences.accept)
        QtCore.QObject.connect(self.buttons, QtCore.SIGNAL("rejected()"), Preferences.reject)
        QtCore.QMetaObject.connectSlotsByName(Preferences)

    def retranslateUi(self, Preferences):
        Preferences.setWindowTitle(QtGui.QApplication.translate("Preferences", "Preferences", None, QtGui.QApplication.UnicodeUTF8))
        self.groupBox.setTitle(QtGui.QApplication.translate("Preferences", "Thumbnail Generation", None, QtGui.QApplication.UnicodeUTF8))
        self.label_2.setText(QtGui.QApplication.translate("Preferences", "Movie Command:", None, QtGui.QApplication.UnicodeUTF8))
        self.label.setText(QtGui.QApplication.translate("Preferences", "Image Command:", None, QtGui.QApplication.UnicodeUTF8))
        self.movie_command.setText(QtGui.QApplication.translate("Preferences", "ffmpeg -y -i $in -f mjpeg -ss $offset -vframes 1 -s svga -an $out", None, QtGui.QApplication.UnicodeUTF8))
        self.image_command.setText(QtGui.QApplication.translate("Preferences", "convert $in $out", None, QtGui.QApplication.UnicodeUTF8))
        self.label_8.setText(QtGui.QApplication.translate("Preferences", "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.0//EN\" \"http://www.w3.org/TR/REC-html40/strict.dtd\">\n"
"<html><head><meta name=\"qrichtext\" content=\"1\" /><style type=\"text/css\">\n"
"p, li { white-space: pre-wrap; }\n"
"</style></head><body style=\" font-family:\'Lucida Grande\'; font-size:13pt; font-weight:400; font-style:normal;\">\n"
"<p style=\" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;\"><span style=\" font-size:9pt;\">The commands to run to generate a thumbnail for a generic image, movie, or photoshop doc respectively.  The string $in will be replaced with the path to the input file.  The string $out will be replaced with the temporary thumbnail file.  The string $offset will be replaced with the offset to the hero frame.</span></p></body></html>", None, QtGui.QApplication.UnicodeUTF8))
        self.groupBox_2.setTitle(QtGui.QApplication.translate("Preferences", "Shotgun", None, QtGui.QApplication.UnicodeUTF8))
        self.label_3.setText(QtGui.QApplication.translate("Preferences", "Shotgun URL:", None, QtGui.QApplication.UnicodeUTF8))
        self.shotgun_url.setText(QtGui.QApplication.translate("Preferences", "http://shotgun.kickass-studios.com/", None, QtGui.QApplication.UnicodeUTF8))
        self.label_4.setText(QtGui.QApplication.translate("Preferences", "Shotgun Script:", None, QtGui.QApplication.UnicodeUTF8))
        self.label_5.setText(QtGui.QApplication.translate("Preferences", "Shotgun Script Key:", None, QtGui.QApplication.UnicodeUTF8))
        self.label_6.setText(QtGui.QApplication.translate("Preferences", "Path to Shotgun API:", None, QtGui.QApplication.UnicodeUTF8))
        self.label_7.setToolTip(QtGui.QApplication.translate("Preferences", "The (optional) name of the field on the Attachment entity to set to the full path of the file being uploaded.  Good to keep track of where the source file for the upload was.", None, QtGui.QApplication.UnicodeUTF8))
        self.label_7.setText(QtGui.QApplication.translate("Preferences", "Field for full path:", None, QtGui.QApplication.UnicodeUTF8))
        self.shotgun_script.setText(QtGui.QApplication.translate("Preferences", "thumb_uploader", None, QtGui.QApplication.UnicodeUTF8))
        self.shotgun_key.setText(QtGui.QApplication.translate("Preferences", "ALONGSTRINGOFNUMBERSANDLETTERSFROMSHOTGUN", None, QtGui.QApplication.UnicodeUTF8))
        self.shotgun_api.setText(QtGui.QApplication.translate("Preferences", "/net/a/place/where/we/put/the/shotgun_api3_preview.py", None, QtGui.QApplication.UnicodeUTF8))
        self.path_field.setText(QtGui.QApplication.translate("Preferences", "sg_path_to_file", None, QtGui.QApplication.UnicodeUTF8))
        self.label_9.setText(QtGui.QApplication.translate("Preferences", "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.0//EN\" \"http://www.w3.org/TR/REC-html40/strict.dtd\">\n"
"<html><head><meta name=\"qrichtext\" content=\"1\" /><style type=\"text/css\">\n"
"p, li { white-space: pre-wrap; }\n"
"</style></head><body style=\" font-family:\'Lucida Grande\'; font-size:13pt; font-weight:400; font-style:normal;\">\n"
"<p style=\" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;\"><span style=\" font-size:10pt;\">The information needed to connect to shotgun.  On upload the value in \"Field for full path\" (if filled out) with be used as the field on Attachment entities to fill out with the path to file you uploaded.</span></p></body></html>", None, QtGui.QApplication.UnicodeUTF8))
        self.groupBox_3.setTitle(QtGui.QApplication.translate("Preferences", "Link Mapping", None, QtGui.QApplication.UnicodeUTF8))
        self.link_map.setHtml(QtGui.QApplication.translate("Preferences", "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.0//EN\" \"http://www.w3.org/TR/REC-html40/strict.dtd\">\n"
"<html><head><meta name=\"qrichtext\" content=\"1\" /><style type=\"text/css\">\n"
"p, li { white-space: pre-wrap; }\n"
"</style></head><body style=\" font-family:\'Lucida Grande\'; font-size:13pt; font-weight:400; font-style:normal;\">\n"
"<p style=\" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;\">Asset: /job_root/*/assets/$type/$name</p>\n"
"<p style=\" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;\">Task: /job_root/*/shots/$entity.shot.name/$entity.task.name</p></body></html>", None, QtGui.QApplication.UnicodeUTF8))
        self.label_10.setText(QtGui.QApplication.translate("Preferences", "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.0//EN\" \"http://www.w3.org/TR/REC-html40/strict.dtd\">\n"
"<html><head><meta name=\"qrichtext\" content=\"1\" /><style type=\"text/css\">\n"
"p, li { white-space: pre-wrap; }\n"
"</style></head><body style=\" font-family:\'Lucida Grande\'; font-size:13pt; font-weight:400; font-style:normal;\">\n"
"<p style=\" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;\"><span style=\" font-size:10pt;\">The values filled out in this table will determine what an uploaded file will be linked to.  The formate is \'Entity Type\': \'Match\'.  Match  is an expression that is run against the full path of the file being uploaded.  Variables starting with \'$\' will be matched against the full path of the file being uploaded.  The name of the variable will be used in a shotgun find_one call to find the entity to link to.  If there are no matches, the file is linked to the Person entity matching the username of the person doing the upload.  These rules are evaluated in order and the first match wins.</span></p></body></html>", None, QtGui.QApplication.UnicodeUTF8))

if __name__ == '__main__':
    app = QtGui.QApplication(sys.argv)
    w = Uploader()
    w.show()
    w.raise_()
    sys.exit(app.exec_())
