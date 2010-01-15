from time import sleep

import tg
import pymongo
from pylons import c, g
from pymongo.bson import ObjectId

from ming import schema
from ming.orm.mapped_class import MappedClass
from ming.orm.property import FieldProperty, RelationProperty, ForeignIdProperty

from pyforge.lib.helpers import nonce
from pyforge.model import Artifact, Message, Filesystem

common_suffix = tg.config.get('forgemail.domain', '.sourceforge.net')

class Forum(Artifact):
    class __mongometa__:
        name='forum'
    type_s = 'Forum'

    parent_id = FieldProperty(schema.ObjectId, if_missing=None)
    shortname = FieldProperty(str)
    name = FieldProperty(str)
    description = FieldProperty(str, if_missing='')
    num_topics = FieldProperty(int, if_missing=0)
    num_posts = FieldProperty(int, if_missing=0)
    subscriptions = FieldProperty({str:bool})

    threads = RelationProperty('Thread')

    def update_stats(self):
        self.num_topics = Thread.query.find(dict(forum_id=self._id)).count()
        self.num_posts = Post.query.find(dict(forum_id=self._id)).count()

    def breadcrumbs(self):
        if self.parent:
            l = self.parent.breadcrumbs()
        else:
            l = []
        return l + [(self.name, self.url())]

    @property
    def email_address(self):
        domain = '.'.join(reversed(self.app.url[1:-1].split('/')))
        return '%s@%s%s' % (self.shortname.replace('/', '.'), domain, common_suffix)

    @property
    def last_post(self):
        q = Post.query.find(dict(
                forum_id=self._id)).sort('timestamp', pymongo.DESCENDING)
        return q.first()

    @property
    def parent(self):
        return Forum.query.get(_id=self.parent_id)

    @property
    def subforums(self):
        return Forum.query.find(dict(parent_id=self._id)).all()
        
    def url(self):
        return self.app.url + self.shortname + '/'
    
    def shorthand_id(self):
        return self.shortname

    def index(self):
        result = Artifact.index(self)
        result.update(type_s=self.type_s,
                      name_s=self.name,
                      text=self.description)
        return result

    def new_thread(self, subject, content, message_id):
        thd = Thread(forum_id=self._id,
                     subject=subject)
        post = Post(_id=message_id,
                    forum_id=self._id,
                    thread_id=thd._id,
                    subject=subject,
                    text=content)
        post.give_access('moderate', user=post.author())
        self.num_topics += 1
        self.num_posts += 1
        g.publish('react', 'Forum.new_thread', dict(
                thread_id=thd._id))
        g.publish('react', 'Forum.new_post', dict(
                post_id=post._id))
        return thd, post

    def subscription(self):
        return self.subscriptions.get(str(c.user._id))

    def delete(self):
        # Delete all the threads, posts, and artifacts
        Thread.query.remove(dict(forum_id=self._id))
        Post.query.remove(dict(forum_id=self._id))
        for md in Attachment.find({
                'metadata.forum_id':self._id}):
            Attachment.remove(md['filename'])
        Artifact.delete(self)

class Thread(Artifact):
    class __mongometa__:
        name='thread'
    type_s = 'Thread'

    _id=FieldProperty(str, if_missing=lambda:nonce(8))
    forum_id = ForeignIdProperty(Forum)
    subject = FieldProperty(str)
    num_replies = FieldProperty(int, if_missing=0)
    num_views = FieldProperty(int, if_missing=0)
    subscriptions = FieldProperty({str:bool})

    forum = RelationProperty(Forum)
    posts = RelationProperty('Post')

    def update_stats(self):
        self.num_replies = Post.query.find(dict(thread_id=self._id)).count() - 1

    @property
    def last_post(self):
        q = Post.query.find(dict(
                thread_id=self._id)).sort('timestamp', pymongo.DESCENDING)
        return q.first()

    @property
    def parent(self):
        return Forum.query.get(_id=self.parent_id)

    def find_posts_by_thread(self, offset, limit):
        q = Post.query.find(dict(forum_id=self.forum_id, thread_id=self._id))
        q = q.sort('_id')
        q = q.skip(offset)
        q = q.limit(limit)
        return q.all()

    def find_posts_by_date(self, offset, limit):
        # Sort the posts roughly in threaded order
        q = Post.query.find(dict(forum_id=self.forum_id, thread_id=self._id))
        q = q.sort('timestamp')
        q = q.skip(offset)
        q = q.limit(limit)
        return q.all()

    def top_level_posts(self):
        return Post.query.find(dict(
                thread_id=self._id,
                parent_id=None))
        
    def url(self):
        return self.forum.url() + 'thread/' + str(self._id) + '/'
    
    def shorthand_id(self):
        return '%s/%s' % (self.forum.shorthand_id(), self._id)

    def index(self):
        result = Artifact.index(self)
        result.update(type_s=self.type_s,
                      name_s=self.subject,
                      views_i=self.num_views,
                      text=self.subject)
        return result

    def subscription(self):
        return self.subscriptions.get(str(c.user._id))

    def delete(self):
        for p in Post.query.find(dict(thread_id=self._id)):
            p.delete()
        Artifact.delete(self)

class Post(Message):
    class __mongometa__:
        name='post'
    type_s = 'Post'

    thread_id = ForeignIdProperty(Thread)
    forum_id = ForeignIdProperty(Forum)
    subject = FieldProperty(str)

    thread = RelationProperty(Thread)
    forum = RelationProperty(Forum)
    attachments = RelationProperty('Attachment')

    @property
    def parent(self):
        return Post.query.get(_id=self.parent_id)

    def url(self):
        return self.thread.url() + self.slug + '/'
    
    def shorthand_id(self):
        return '%s#%s' % (self.thread.shorthand_id(), self.slug)

    def index(self):
        result = Message.index(self)
        result.update(type_s=self.type_s,
                      name_s=self.subject)
        return result

    def reply_subject(self):
        if self.subject.lower().startswith('re:'):
            return self.subject
        else:
            return 'Re: ' + self.subject

    def reply_text(self):
        l = [ '%s wrote:' % self.author().display_name ]
        l += [ '> ' + line for line in self.text.split('\n') ]
        return '\n'.join(l)

    def reply(self, subject, text, message_id):
        result = Message.reply(self)
        result._id = message_id
        result.forum_id = self.forum_id
        result.thread_id = self.thread_id
        result.subject = subject
        result.text = text
        result.give_access('moderate', user=result.author())
        g.publish('react', 'Forum.new_post', dict(
                post_id=result._id))
        return result

    @classmethod
    def create_post_threads(cls, posts):
        result = []
        post_index = {}
        for p in posts:
            pi = dict(post=p, children=[])
            post_index[p._id] = pi
            if p.parent_id in post_index:
                post_index[p.parent_id]['children'].append(pi)
            else:
                result.append(pi)
        return result

    @property
    def attachments(self):
        return Attachment.find({
                'metadata.message_id':self._id})

    def attachment_url(self, file_info):
        return self.forum.url() + 'attachment/' + file_info['filename']

    def attachment_filename(self, file_info):
        return file_info['metadata']['filename']

    def delete(self):
        for md in Attachment.find({
                'metadata.message_id':self._id}):
            Attachment.remove(md['filename'])
        Message.delete(self)

class Attachment(Filesystem):
    class __mongometa__:
        name='attachment'
        indexes = [
            'metadata.forum_id',
            'metadata.message_id',
            'metadata.filename' ]

    @classmethod
    def save(cls, filename, content_type,
             forum_id, message_id, content):
        with cls.open(str(ObjectId()), 'w') as fp:
            fp.content_type = content_type
            fp.metadata = dict(forum_id=forum_id,
                               message_id=message_id,
                               filename=filename)
            fp.write(content)

    @classmethod
    def load(cls, id, offset=0, limit=-1):
        with cls.open(id, 'r') as fp:
            if offset:
                fp.seek(offset)
            return fp.read(limit)
    
MappedClass.compile_all()
