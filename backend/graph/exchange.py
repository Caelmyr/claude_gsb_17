"""
图谱数据交换模块

导出格式遵循易与其他系统交换的 JSON 图结构：
{
  "format": "knowledge-graph",
  "version": "1.0",
  "metadata": {...},
  "entities": [
    {"id": "...", "text": "实体", "type": "PERSON", "properties": {}, "count": 1}
  ],
  "relations": [
    {"subject": "主体", "predicate": "关系", "object": "客体",
     "subject_type": "PERSON", "object_type": "ORG",
     "properties": {}, "count": 1}
  ]
}

导入时同时兼容常见字段命名：entities/nodes、relations/links/edges/triples。
"""
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Tuple

from backend.utils.config import ENTITY_TYPES


GRAPH_FORMAT = 'knowledge-graph'
GRAPH_VERSION = '1.0'

TYPE_ALIASES = {
    'PERSON': 'PERSON',
    'PER': 'PERSON',
    '人物': 'PERSON',
    '人': 'PERSON',
    '人名': 'PERSON',
    'ORG': 'ORG',
    'ORGANIZATION': 'ORG',
    '组织': 'ORG',
    '机构': 'ORG',
    'LOCATION': 'LOCATION',
    'LOC': 'LOCATION',
    'GPE': 'LOCATION',
    '地点': 'LOCATION',
    '位置': 'LOCATION',
    '地址': 'LOCATION',
    'TIME': 'TIME',
    'DATE': 'TIME',
    '时间': 'TIME',
    '日期': 'TIME',
    'CONCEPT': 'CONCEPT',
    '概念': 'CONCEPT',
    'EVENT': 'EVENT',
    '事件': 'EVENT',
    'OTHER': 'OTHER',
    'MISC': 'OTHER',
    '其他': 'OTHER',
}

ENTITY_KEYS = ('text', 'name', 'label', 'value', 'title')
TYPE_KEYS = ('type', 'entity_type', 'entityType', 'group', 'category', 'kind')
SUBJECT_KEYS = ('subject', 'source', 'from', 'head', 'start', 'source_node')
PREDICATE_KEYS = ('predicate', 'relation', 'label', 'type', 'name', 'relationship')
OBJECT_KEYS = ('object', 'target', 'to', 'tail', 'end', 'target_node')


class GraphExchange:
    """图谱 JSON 导入导出工具"""

    @staticmethod
    def normalize_type(entity_type) -> str:
        """归一化实体类型，无法识别的外部类型归入 OTHER"""
        if entity_type is None:
            return 'OTHER'

        raw_type = str(entity_type).strip()
        if not raw_type:
            return 'OTHER'

        upper_type = raw_type.upper()
        if upper_type in ENTITY_TYPES:
            return upper_type
        return TYPE_ALIASES.get(raw_type, TYPE_ALIASES.get(upper_type, 'OTHER'))

    @staticmethod
    def build_export(storage) -> Dict:
        """从存储层生成标准导出对象"""
        stats = storage.get_statistics()
        entities = []
        for entity in storage.get_all_entities():
            entities.append({
                'id': entity.get('id'),
                'text': entity['text'],
                'type': entity['type'],
                'properties': entity.get('properties', {}),
                'count': entity.get('count', 1)
            })

        relations = []
        for relation in storage.get_all_relations():
            relations.append({
                'subject': relation['subject'],
                'subject_type': relation.get('subject_type', 'OTHER'),
                'predicate': relation['predicate'],
                'object': relation['object'],
                'object_type': relation.get('object_type', 'OTHER'),
                'properties': relation.get('properties', {}),
                'count': relation.get('count', 1)
            })

        entities.sort(key=lambda item: (item['type'], item['text']))
        relations.sort(key=lambda item: (
            item['subject'], item['predicate'], item['object']
        ))

        return {
            'format': GRAPH_FORMAT,
            'version': GRAPH_VERSION,
            'metadata': {
                'exported_at': datetime.now().isoformat(timespec='seconds'),
                'source': 'knowledge-graph-qa',
                'statistics': stats
            },
            'entities': entities,
            'relations': relations
        }

    @staticmethod
    def parse_import(data: Dict) -> Tuple[List[Dict], List[Dict], Dict]:
        """
        解析并规范化外部图谱 JSON。

        返回 (entities, relations, parse_summary)。重复实体和关系会在本阶段先合并，
        存储层还会与当前图谱再次去重合并。
        """
        if not isinstance(data, dict):
            raise ValueError('导入文件内容必须是 JSON 对象')

        raw_entities = data.get('entities')
        if raw_entities is None:
            raw_entities = data.get('nodes', [])
        raw_relations = data.get('relations')
        if raw_relations is None:
            raw_relations = data.get('links')
        if raw_relations is None:
            raw_relations = data.get('edges')
        if raw_relations is None:
            raw_relations = data.get('triples', [])

        if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
            raise ValueError('entities/nodes 和 relations/links/edges/triples 必须是数组')

        errors = []
        entities_by_text = OrderedDict()
        entity_ids = {}

        def remember_entity_id(entity_id, text):
            if entity_id is None:
                return
            entity_id = str(entity_id).strip()
            if entity_id:
                entity_ids[entity_id] = text

        def add_entity(text, entity_type='OTHER', properties=None, count=1,
                       entity_id=None):
            text = GraphExchange._clean_text(text)
            entity_type = GraphExchange.normalize_type(entity_type)
            properties = GraphExchange._clean_properties(properties)
            count = GraphExchange._clean_count(count)

            if text in entities_by_text:
                entity = entities_by_text[text]
                entity['count'] += count
                entity['properties'].update(properties)
                return entity, True

            entity = {
                'text': text,
                'type': entity_type,
                'properties': properties,
                'count': count
            }
            entities_by_text[text] = entity
            remember_entity_id(entity_id, text)
            remember_entity_id(text, text)
            return entity, False

        for index, item in enumerate(raw_entities):
            try:
                if isinstance(item, (list, tuple)):
                    if len(item) < 1:
                        raise ValueError('实体数组至少需要包含实体文本')
                    text = item[0]
                    entity_type = item[1] if len(item) > 1 else 'OTHER'
                    properties = item[2] if len(item) > 2 else {}
                    count = item[3] if len(item) > 3 else 1
                    entity_id = None
                elif isinstance(item, dict):
                    text = GraphExchange._first_value(item, ENTITY_KEYS)
                    if text is None:
                        raise ValueError('实体缺少 text/name/label 字段')
                    entity_type = GraphExchange._first_value(item, TYPE_KEYS, 'OTHER')
                    properties = item.get('properties', item.get('attributes', {}))
                    count = item.get('count', 1)
                    entity_id = item.get('id')
                else:
                    raise ValueError('实体必须是对象或 [文本, 类型] 数组')

                add_entity(text, entity_type, properties, count, entity_id)
            except (ValueError, TypeError) as exc:
                errors.append(f'entities[{index}]: {exc}')
                if len(errors) >= 20:
                    break

        if errors:
            raise ValueError('；'.join(errors))

        relations_by_key = OrderedDict()

        def resolve_endpoint(value, explicit_type, position):
            if isinstance(value, dict):
                endpoint_ref = value.get('id')
                if endpoint_ref is None:
                    endpoint_ref = GraphExchange._first_value(value, ENTITY_KEYS)
                endpoint_type = GraphExchange._first_value(value, TYPE_KEYS, explicit_type)
                return resolve_endpoint(endpoint_ref, endpoint_type, position)

            if isinstance(value, (list, tuple)):
                if len(value) < 1:
                    raise ValueError(f'{position}端点不能为空')
                endpoint_type = value[1] if len(value) > 1 else explicit_type
                return resolve_endpoint(value[0], endpoint_type, position)

            if value is None:
                raise ValueError(f'缺少{position}端点')

            ref = str(value).strip()
            if not ref:
                raise ValueError(f'{position}端点不能为空')

            text = entity_ids.get(ref, ref)
            if text in entities_by_text:
                return text, entities_by_text[text]['type']

            entity_type = GraphExchange.normalize_type(explicit_type)
            add_entity(text, entity_type)
            return text, entity_type

        for index, item in enumerate(raw_relations):
            try:
                if isinstance(item, (list, tuple)):
                    if len(item) < 3:
                        raise ValueError('关系数组至少需要包含 [主体, 关系, 客体]')
                    subject_ref, predicate, object_ref = item[:3]
                    subject_type = item[3] if len(item) > 3 else 'OTHER'
                    object_type = item[4] if len(item) > 4 else 'OTHER'
                    properties = item[5] if len(item) > 5 else {}
                    count = item[6] if len(item) > 6 else 1
                elif isinstance(item, dict):
                    subject_ref = GraphExchange._first_value(item, SUBJECT_KEYS)
                    predicate = GraphExchange._first_value(item, PREDICATE_KEYS)
                    object_ref = GraphExchange._first_value(item, OBJECT_KEYS)
                    subject_type = item.get('subject_type', item.get('source_type', 'OTHER'))
                    object_type = item.get('object_type', item.get('target_type', 'OTHER'))
                    properties = item.get('properties', item.get('attributes', {}))
                    count = item.get('count', 1)
                else:
                    raise ValueError('关系必须是对象或 [主体, 关系, 客体] 数组')

                predicate = GraphExchange._clean_text(predicate, '关系类型')
                subject, subject_type = resolve_endpoint(subject_ref, subject_type, '主体')
                obj, object_type = resolve_endpoint(object_ref, object_type, '客体')
                properties = GraphExchange._clean_properties(properties)
                count = GraphExchange._clean_count(count)
                key = (subject, predicate, obj)

                if key in relations_by_key:
                    relation = relations_by_key[key]
                    relation['count'] += count
                    relation['properties'].update(properties)
                else:
                    relations_by_key[key] = {
                        'subject': subject,
                        'subject_type': subject_type,
                        'predicate': predicate,
                        'object': obj,
                        'object_type': object_type,
                        'properties': properties,
                        'count': count
                    }
            except (ValueError, TypeError) as exc:
                errors.append(f'relations[{index}]: {exc}')
                if len(errors) >= 20:
                    break

        if errors:
            raise ValueError('；'.join(errors))

        parse_summary = {
            'parsed_entities': len(entities_by_text),
            'parsed_relations': len(relations_by_key)
        }
        return list(entities_by_text.values()), list(relations_by_key.values()), parse_summary

    @staticmethod
    def _first_value(data: Dict, keys: Tuple[str, ...], default=None):
        for key in keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return value
        return default

    @staticmethod
    def _clean_text(value, field_name='实体文本'):
        if value is None:
            raise ValueError(f'{field_name}不能为空')
        text = str(value).strip()
        if not text:
            raise ValueError(f'{field_name}不能为空')
        return text

    @staticmethod
    def _clean_properties(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError('properties 必须是对象')
        return {str(key): val for key, val in value.items() if key is not None}

    @staticmethod
    def _clean_count(value):
        try:
            count = int(value)
        except (TypeError, ValueError):
            raise ValueError('count 必须是正整数')
        if count < 1:
            raise ValueError('count 必须是正整数')
        return count
