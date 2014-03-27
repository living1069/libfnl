"""
.. py:module:: fnl.nlp.genia.tagger
   :synopsis: A subprocess wrapper for the GENIA Tagger.

.. moduleauthor:: Florian Leitner <florian.leitner@gmail.com>
.. License: GNU Affero GPL v3 (http://www.gnu.org/licenses/agpl.html)
"""

import logging
import os
from subprocess import Popen, PIPE
from threading import Thread

from fnl.nlp.token import Token

GENIATAGGER_DIR = os.environ.get('GENIATAGGER_DIR', '/usr/local/share/geniatagger')
"""
The directory containing the ``geniatagger`` binary and ``morphdic`` directory,
defaulting to ``/usr/local/share/geniatagger``, or as set in the environment.
"""

GENIATAGGER = "geniatagger"
"""
The default path of the ``geniatagger``. If the GENIA Tagger is on the ``PATH``,
the name of the binary will do.
"""

class GeniaTagger(object):
    """
    A subprocess wrapper for the GENIA Tagger.
    """

    L = logging.getLogger("GeniaTagger")

    def __init__(self, binary:str=GENIATAGGER,
                 morphdic_dir:str=GENIATAGGER_DIR,
                 tokenize:bool=True):
        """
        :param binary: The path or name (if in ``$PATH``) of the geniatagger
                       binary.
        :param morphdic_dir: The directory where the morphdic directory is
                             located (ie., **not** including the ``morphdic``
                             directory itself).
        :param tokenize: If ``False``, geniatagger is run without
                         tokenization (ie., with the ``-nt`` flag).
        """
        if os.path.isabs(binary): GeniaTagger._checkPath(binary, os.X_OK)
        GeniaTagger._checkPath("{}/morphdic".format(morphdic_dir), os.R_OK)
        args = [binary] if tokenize else [binary, '-nt']
        self.L.debug("starting '%s'" % ' '.join(args))
        self.L.debug("in directory '%s'", morphdic_dir)
        self._proc = Popen(args, cwd=morphdic_dir,
                          stdin=PIPE, stdout=PIPE, stderr=PIPE)
        debug_msgs = Thread(target=GeniaTagger._logStderr,
                            args=(self.L, self._proc.stderr))
        debug_msgs.start()

    @staticmethod
    def _checkPath(path, acc_code):
        assert os.path.exists(path) and os.access(path, acc_code), \
        "invalid path %s" % path

    @staticmethod
    def _logStderr(logger, stderr):
        while True:
            line = stderr.readline().decode()
            if line: logger.debug("STDERR: %s", line.strip())
            else: break

    def __del__(self):
        self.L.debug("terminating")
        if hasattr(self, "_proc"): self._proc.terminate()

    def __iter__(self):
        return self

    def __next__(self):
        status = self._proc.poll()

        if status is not None:
            raise RuntimeError("geniatagger exited with %i" % status * -1)

        self.L.debug('reading token')
        line = self._proc.stdout.readline()
        self.L.debug('fetched token')
        # noinspection PyUnresolvedReferences
        line = line.decode().strip()
        if not line: raise StopIteration
        items = line.split('\t')
        self.L.debug('raw result: %s', items)
        return Token(*items)

    # To make this module compatible with Python 2:
    next = __next__

    def send(self, sentence:str):
        """
        Send a single *sentence* (w/o newline) to the tagger.
        """
        self.L.debug('sending sentence: "%s"', sentence)
        self._proc.stdin.write(sentence.encode())
        self._proc.stdin.write(b"\n")
        self._proc.stdin.flush()