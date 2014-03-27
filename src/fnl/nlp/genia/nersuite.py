"""
.. py:module:: fnl.nlp.genia.nersuite
   :synopsis: A subprocess wrapper for the NER Suite tagger.

.. moduleauthor:: Florian Leitner <florian.leitner@gmail.com>
.. License: GNU Affero GPL v3 (http://www.gnu.org/licenses/agpl.html)
"""

import logging
import os
from subprocess import Popen, PIPE
from threading import Thread

from fnl.nlp.token import Token

NERSUITE_TAGGER = "nersuite"
"""
The default path of the ``nersuite``. If the NER Suite tagger is on the ``PATH``,
the name of the binary will do.
"""

class NerSuite(object):
    """
    A subprocess wrapper for the NER Suite tagger.
    """

    L = logging.getLogger("NerSuite")

    def __init__(self, model:str, binary:str=NERSUITE_TAGGER):
        """
        :param model: The path to the model to use by the tagger.
        :param binary: The path or name (if in ``$PATH``) of the nersuite binary.
d        """
        if os.path.isabs(binary): NerSuite._checkPath(binary, os.X_OK)
        NerSuite._checkPath(model, os.R_OK)
        args = [binary, 'tag', '-m', model]
        self.L.debug("starting '%s'" % ' '.join(args))
        self._proc = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        debug_msgs = Thread(target=NerSuite._logStderr,
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
            raise RuntimeError("nersuite exited with status %i" % status * -1)

        self.L.debug('reading token')
        # noinspection PyUnresolvedReferences
        line = self._proc.stdout.readline().decode('ASCII').strip()
        self.L.debug('fetched line "%s"', line)
        if not line: raise StopIteration
        items = line.split('\t')
        self.L.debug('raw result: %s', items)
        return Token(*items[2:])

    # To make this module compatible with Python 2:
    next = __next__

    def send(self, tokens:[Token]):
        """
        Send a single sentence as a list of tokens to the tagger.
        """
        self.L.debug('sending tokens for: "%s"', '" "'.join([t[0] for t in tokens]))

        for t in tokens:
            self._proc.stdin.write("1\t2\t".encode('ASCII'))
            self._proc.stdin.write('\t'.join(t[0:-1]).encode('ASCII'))
            self._proc.stdin.write("\n".encode('ASCII'))

        self._proc.stdin.write("\n".encode('ASCII'))
        self._proc.stdin.flush()