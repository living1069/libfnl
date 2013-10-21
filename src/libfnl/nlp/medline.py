"""
.. py:module:: medline
   :synopsis: Download and parse MEDLINE XML records.

.. moduleauthor: Florian Leitner <florian.leitner@gmail.com>
.. License: GNU Affero GPL v3 (http://www.gnu.org/licenses/agpl.html)

This module provides functions to fetch and parse NLM PubMed/MEDLINE XML
records, dump them into a CouchDB, and attach external content articles (eg.,
full-text articles) to them.
"""
import logging
import os
from collections import defaultdict
from io import StringIO
from http.client import HTTPResponse # function annotation only
import re
from time import sleep, time
from unicodedata import normalize
from urllib.request import build_opener
from xml.etree.ElementTree import iterparse, tostring, Element
from logging import getLogger
from datetime import date, timedelta
from libfnl.couch.broker import Database
from libfnl.nlp.extract import Extract
from libfnl.nlp.text import Text

__all__ = ['Attach', 'Dump', 'Fetch', 'Parse']

LOGGER = getLogger('medline')

MONTHS_SHORT = (None, 'jan', 'feb', 'mar', 'apr', 'may', 'jun',
                'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
# to translate three-letter month strings to integers

URL_OPENER = build_opener()
# to avoid having to build a new opener for each request

EUTILS_URL = 'http://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?tool=libfnl&db=pubmed&retmode=xml&rettype=medline&id='
"""
The base URL where eUtils requests for MEDLINE records should be made.

Multiple PMIDs are joined comma-separated and appended to this URL.
"""

FETCH_SIZE = 100
"""
Number of records that can be fetched from eUtils in one request.
"""

SKIPPED_ELEMENTS = ('OtherID', 'OtherAbstract', 'SpaceFlightMission',
                    'GeneralNote', 'NameID', 'ELocationID', 'CitationSubset')
"""
Ignored tags in MedlineCitation elements that are never parsed.
Note that OtherID is treated separately and parsed into the ArticleIds
dictionary.
"""

ABSTRACT_ELEMENTS = [
    'AbstractText',
    'Background',
    'Objective',
    'Methods',
    'Results',
    'Conclusions',
]
"""
List of keys found in Article{1}->Abstract{0,1}, in order of how they should
be added to a **text** member.
"""

ABSTRACT_FILE = 'abstract.txt'
"""
Name of file used for attaching abstracts to Couch documents.
"""

############
# IMPORTER #
############

def Dump(pmids:list([str]), db:Database, update:bool=False,
         force:bool=False) -> int:
    """
    :param pmids: The list of PMIDs to fetch (but existing articles will be
        ignored).
    :param db: A Couch :class:`.Database` instance.
    :param update: Update existing articles, if appropriate (see above).
    :param force: If ``True``, force the *update* of all existing articles.
    :return: Number of dumped documents and a list of failed PMIDs.
    """
    last_access = 0.0
    processed_docs = 0
    failed_docs = []

    for idx in range(0, len(pmids), FETCH_SIZE):
        if update:
            #noinspection PyTypeChecker
            existing = { p: db[p] for p in pmids[idx:idx + FETCH_SIZE]
                         if p in db }

            if force:
                updated = existing
                query = pmids[idx:idx + FETCH_SIZE]
            else:
                updated = dict(filter(NeedsUpdate, existing.items()))
                query = [ p for p in pmids[idx:idx + FETCH_SIZE]
                          if (p in updated or p not in existing) ]
        else:
            updated = {}
            query = [ p for p in pmids[idx:idx + FETCH_SIZE] if p not in db ]

        if len(query):
            # the Gatekeeper: only make eUtils requests every 3 sec
            sleep(max(last_access - time() + 3, 0))
            last_access = time()
            stream = Fetch(query)
            for saved, pmid, r in db.bulk(MakeDocuments(stream, updated)):
                if not saved:
                    LOGGER.error('saving %s failed: %s', pmid, str(r))
                    failed_docs.append(pmid)
                else:
                    processed_docs += 1

    return processed_docs, failed_docs


def NeedsUpdate(item:(str, dict)) -> bool:
    one_week = timedelta(7)
    today = date.today()
    doc = item[1]
    created = DateFromIsoDatetime(doc['created'])

    if today - created < one_week:
        return False

    if doc['Status'] in ('In-Data-Review', 'In-Process'):
        return True

    one_year = timedelta(365)
    created = DateFromIsoDate(doc['DateCreated'])

    if today - created > one_year:
        # select records that are one year old, but not more than ten
        if today - created < 10 * one_year:
            # ignoring 'ancient' records, check they have all three stamps
            for stamp in ('DateRevised', 'DateCompleted'):
                if stamp not in doc: return True
    else:
        # record is new, check if it has been completed
        if 'DateCompleted' not in doc:
            return True

    return False


def DateFromIsoDatetime(isodatetime:str) -> date:
    #noinspection PyTypeChecker
    return DateFromIsoDate(isodatetime.split('T')[0])


def DateFromIsoDate(isodate:str) -> date:
    y, m, d = isodate.split('-')
    return date(int(y), int(m), int(d))


def MakeDocuments(stream, old_revisions:dict=None) -> iter([dict]):
    """
    Parse MEDLINE records from an XML stream.

    The resulting iterator can simply be sent to :meth:`.couch.Database.bulk`
    to be inserted in a database. However, note that if you try to bulk load
    too many records at once, this might be rather dangerous. A few 100s or
    even up to about 10k records should not matter, although.

    :param stream: A readable, file-like object with the raw XML containing
        Medline Citations.
    :param old_revisions: A dictionary of CouchDB MEDLINE documents that are
        to be updated if found on the incoming XML stream. The dictionary's
        keys should be the PMIDs of the document, the values the document
        itself. Optional.
    :return: A document iterator.
    """
    for doc in Parse(stream):
        doc['text'] = MakeText(doc)
        #noinspection PyTypeChecker
        pmid = doc['_id'] = doc['PMID'][0]

        if old_revisions and pmid in old_revisions:
            old = old_revisions[pmid]

            #noinspection PyTypeChecker
            if old['text'] != doc['text']:
                LOGGER.error('text for %s changed; not updating', pmid)
                continue

            doc['created'] = old['created']
            doc['_rev'] = old['_rev']

        yield doc


def MakeText(raw_json:dict) -> Text:
    article = raw_json['Article']
    text = normalize('NFC', article['ArticleTitle'])

    if 'Abstract' in article:
        abstract = article['Abstract']
        buffer = StringIO()
        buffer.write(text)

        for section in ABSTRACT_ELEMENTS:
            if section in abstract:
                buffer.write('\n\n')
                buffer.write(normalize('NFC', abstract[section]))

        text = buffer.getvalue()

    return text


##############
# DOWNLOADER #
##############

def Fetch(pmids:list, timeout:int=60) -> HTTPResponse:
    """
    :param pmids: a list of up to 100 PMIDs, as values that can be cast to
        string.
    :param timeout: Number of seconds to wait for a response.
    :return: An XML stream.
    :raises IOError: If the stream from eUtils cannot be opened.
    :raises urllib.error.URLError: If the connection to the eUtils URL cannot
        be made.
    :raises socket.timout: If *timeout* seconds have passed before a response
        arrives.
    """
    assert len(pmids) <= FETCH_SIZE, 'too many PMIDs'
    #noinspection PyTypeChecker
    url = EUTILS_URL + ','.join(map(str, pmids))
    LOGGER.debug('fetching MEDLINE records from %s', url)
    return URL_OPENER.open(url, timeout=timeout)

##########
# PARSER #
##########

def Parse(xml_stream) -> iter([dict]):
    """
    :param xml_stream: A stream as returned by :func:`.Fetch` or XML found
        in the MEDLINE distribution XML files.
    :raise AssertionError: If parsing of the XML does not go as expected, but
        only in non-optimized mode.
    """
    record = None
    last = None

    for unused, element in iterparse(xml_stream):
        if element.tag == 'MedlineCitation':
            if record is not None:
                assert 'PMID' in record, \
                    'No PMID in record XML:\n{}'.format(tostring(last))
                yield record

            try:
                record = ParseElement(element)
                other_ids = ParseOtherIds(element)
                if other_ids: record['ArticleIds'] = other_ids
            except Exception as ex:
                LOGGER.fatal('parsing %s failed: %s; XML:\n%s',
                             element.tag, ex, tostring(element))
                record = None
                raise
            else:
                last = element
        elif element.tag == 'PubmedArticle':
            article_id_list = element.find('PubmedData/ArticleIdList')

            if article_id_list is not None and record is not None:
                article_ids = ParseArticleIdList(article_id_list)

                if 'ArticleIds' in record:
                    record['ArticleIds'].update(article_ids)
                else:
                    record['ArticleIds'] = article_ids

    if record is not None:
        assert 'PMID' in record, \
            'No PMID in record XML:\n{}'.format(tostring(last))
        yield record

# Main Element Parser and Regular Elements

def ParseElement(element):
    tag = element.tag

    if tag.endswith('List'):
        return ParseElementList(element)
    elif tag.startswith('Date') or tag.endswith('Date'):
        return ParseDateElement(element)
    elif tag == 'MeshHeading':
        return ParseMeshHeading(element)
    elif tag == 'Abstract':
        return ParseAbstract(element)
    elif tag == 'PMID':
        LOGGER.debug('parsing PMID=%s', element.text)
        return element.text.strip(), int(element.get('Version', 1))
    elif tag == 'ISSN':
        if element.text is None or not element.text.strip():
            return None
        else:
            return element.get('IssnType'), element.text.strip()
    else:
        return ParseRegularElement(element)


def ParseRegularElement(element):
    content = dict(ParseChildren(element))
    attributes = dict(element.items())

    if not content and not attributes:
        if element.text is None:
            LOGGER.debug('empty element %s', element.tag)
            return None
        else:
            return element.text.strip()

    if attributes and not content:
        if len(attributes) == 1 and list(attributes.keys())[0].endswith('YN'):
            try:
                return element.text.strip()
            except AttributeError:
                LOGGER.fatal('parsing %s failed: no text content; XML:\n%s',
                             element.tag, tostring(element))
                raise

        # done everything possible to avoid it...
        # without further options, put the element inside itself
        content[element.tag] = element.text.strip()


    if attributes: content.update(dict(attributes))

    for k, v in content.items():
        if v is None: del[k]

    assert content, 'Empty element; XML:\n{}'.format(tostring(element))
    return content


def ParseChildren(parent):
    known_tags = []
    languages = []

    for child in parent.getchildren():
        if child.tag in SKIPPED_ELEMENTS: continue

        if child.tag == 'ArticleDate':
            # Multiple article dates can exist; to avoid overwriting any,
            # use the DateType attribute as prefix of the tag/key.
            # The default, according to the DTD, is 'Electronic'.
            tag = '{}ArticleDate'.format(child.get('DateType', 'Electronic'))
        elif child.tag == 'Language':
            content = ParseElement(child)

            if content is not None:
                languages.append(content)

            continue
        else:
            tag = child.tag

        assert tag not in known_tags, \
            'Duplicate child {}; XML::\n{}'.format(tag, tostring(parent))

        content = ParseElement(child)

        if content is not None:
            yield tag, content

        known_tags.append(tag)

    if languages:
        yield 'LanguageList', languages


def ParseElementList(list_element:Element):
    return [ ParseElement(element) for element in list_element.getchildren() ]


def ParseDateElement(date_element):
    if date_element.tag == 'MedlineDate':
        return date_element.text.strip()
    try:
        year = int(date_element.find('Year').text)
        month_text = date_element.find('Month').text.strip()
        try:
            month = int(month_text)
        except ValueError:
            month = MONTHS_SHORT.index(month_text.lower())

        try:
            day = int(date_element.find('Day').text)
            return date(year, month, day)
        except (AttributeError, ValueError):
            return date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        # Dates enclosing a MedlineDate can never be parsed to regular dates
        if date_element.find('MedlineDate') is None:
            # PubDate quite frequently only has a year - suppress msg
            if not (date_element.tag == 'PubDate' and
                    date_element.find('Year') is not None):
                msg = 'ParseDateElement: %s not recognized; text XML:\n%s'
                LOGGER.warn(msg, date_element.tag,
                            tostring(date_element).strip())

        return dict(ParseChildren(date_element))

# Special Cases

def ParseArticleIdList(element):
    articles = {}

    for article in element.findall('ArticleId'):
        id_type = article.get('IdType')

        if id_type in articles:
            if re.match('\d[\d\.]+\/.+', article.text.strip()) and \
               'doi' not in articles:
                id_type = 'doi'
            else:
                msg = 'duplicate {} ArticleId'.format(id_type)
                LOGGER.error(msg)
                assert False, '{}; XML:\n{}'.format(msg, tostring(element))

        articles[id_type] = article.text.strip()

    assert articles, 'Empty ArticleIdList; XML:\n{}'.format(tostring(element))
    return articles


def ParseOtherIds(element):
    others = {}

    for other_id in element.findall('OtherID'):
        source = other_id.get('Source').lower()
        if source == 'nlm' and other_id.text.startswith('PMC'): source = 'pmc'

        if source in others:
            msg = 'duplicate OtherID Source {}'.format(source)
            LOGGER.error(msg)
            assert False, '{}; XML:\n{}'.format(msg, tostring(element))

        others[source] = other_id.text.strip()

    return others


def ParseMeshHeading(element):
    ParseMesh = lambda e: (e.text.strip(), e.get('MajorTopicYN') == 'Y')

    descriptor = ParseMesh(element.find('DescriptorName'))
    qualifiers = dict(ParseMesh(e) for e in element.findall('QualifierName'))
    mesh_heading = (descriptor[1], descriptor[0], qualifiers)
    return mesh_heading


def ParseAbstract(element):
    abstract = {}

    for abstract_text in element.findall('AbstractText'):
        cat = abstract_text.get('NlmCategory', 'UNLABELLED').capitalize()

        if cat == 'Unlabelled': cat = 'AbstractText'

        try:
            if cat in abstract:
                LOGGER.info('Duplicate %s in Abstract; XML:\n%s',
                            cat, tostring(element))
                abstract[cat] += '\n' + abstract_text.text.strip()
            else:
                abstract[cat] = abstract_text.text.strip()
        except AttributeError:
            if abstract_text.text is None:
                LOGGER.info('Empty %s element in Abstract; XML:\n%s',
                             abstract_text.tag, tostring(element))
            else:
                raise

    copyright_info = element.find('CopyrightInformation')

    if copyright_info is not None:
        abstract['CopyrightInformation'] = copyright_info.text.strip()

    return abstract

#############
# RELATIONS #
#############

def Attach(filenames:list, db:Database, encoding:str='utf-8',
           force:bool=False):
    """
    :param filenames: A list of file name strings.
    :param db: A :class:`.Database` instance to which the files are saved.
    :param encoding: The (charset) encoding of the files to attach.
    :param force: Replace any existing article. Note this does not erase any
        ``pmids`` already stored on the article.
    :raise IOError: If the file cannot be read.
    :return: A dictionary of PMIDs, with a list of the files' document IDs that
        were created for them.
    """
    logger = logging.getLogger('Attach')
    results = defaultdict(list)

    for fn in filenames:
        #noinspection PyBroadException
        try:
            text = Extract(fn, encoding)
        except IOError:
            logger.error('could not read %s', fn)
            continue
        except:
            logger.exception('failed to extract %s', fn)
            continue

        base = os.path.basename(fn)
        pmid, ext = os.path.splitext(base)
        att_id = text.base64digest
        modified = False

        #noinspection PyTypeChecker
        if att_id in db:
            attachment = db[att_id]

            try:
                pmid_list = attachment['xrefs']
            except KeyError:
                logger.warn('attachment %s (%s) had no PMIDs', att_id, base)
                pmid_list = attachment['xrefs'] = []

            if pmid in pmid_list and not force:
                logger.info('%s already attached (%s)', fn, att_id)
            else:
                pmid_list.append(pmid)

                if force:
                    logger.info('updating %s (%s)', att_id, base)
                    attachment['sections'] = text.tagsAsDict()
                    attachment['text'] = str(text)
                    modified = True

                db[att_id] = attachment
        else:
            logger.debug('creating %s (%s)', att_id, base)
            db[att_id] = {
                'text': str(text),
                'sections': text.tagsAsDict(),
                'xrefs': [pmid],
            }
            modified = True

        if modified:
            db.saveAttachment(att_id, open(fn, 'rb').read(),
                              'raw{}'.format(ext), charset=encoding)

        results[pmid].append(att_id)

    return results