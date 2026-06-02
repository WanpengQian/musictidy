"""beets 表的只读 SQLAlchemy 映射.

beets 自己 own library.db；我们 ATTACH 它后用 schema 名 `beets`. 这里只
做查询用的薄映射，不写。如果 beets 升级改了 schema 这里跟着调即可，
不影响 beets 自己。

参考：beets 源码 beets/library.py 里 Item / Album 的字段定义。
"""

# TODO: 用 SQLAlchemy Core Table 定义而不是 ORM Mapper，因为我们只读
# 大致字段：
#
# items:
#   id, path, length, bitrate, format, samplerate, bitdepth,
#   title, artist, albumartist, album, track, tracktotal, disc, year,
#   mb_trackid, mb_albumid, mb_artistid, mb_albumartistid, mb_releasegroupid,
#   added
#
# albums:
#   id, albumartist, album, year,
#   mb_albumid, mb_albumartistid, mb_releasegroupid,
#   artpath
