"""
图谱数据交换模块 - 图谱导入导出（标准JSON格式）
"""
from datetime import datetime
from typing import Dict
from backend.graph.storage import GraphStorage
from backend.utils.config import ENTITY_TYPES


class GraphExchange:
    """图谱导入导出管理器"""

    FORMAT_NAME = 'knowledge-graph'
    FORMAT_VERSION = '1.0'

    def __init__(self, storage: GraphStorage):
        self.storage = storage

    # ==================== 导出 ====================

    def export_graph(self) -> Dict:
        """导出完整图谱为标准JSON格式"""
        entities = []
        for entity in self.storage.get_all_entities():
            entities.append({
                'id': entity['id'],
                'text': entity['text'],
                'type': entity['type'],
                'count': entity.get('count', 1),
                'properties': entity.get('properties', {})
            })

        relations = []
        for relation in self.storage.get_all_relations():
            relations.append({
                'subject': relation['subject'],
                'subject_type': relation['subject_type'],
                'predicate': relation['predicate'],
                'object': relation['object'],
                'object_type': relation['object_type'],
                'properties': relation.get('properties', {})
            })

        return {
            'format': self.FORMAT_NAME,
            'version': self.FORMAT_VERSION,
            'exported_at': datetime.now().isoformat(),
            'statistics': self.storage.get_statistics(),
            'entities': entities,
            'relations': relations
        }

    # ==================== 导入 ====================

    def import_graph(self, data: Dict) -> Dict:
        """导入图谱数据，自动对实体和关系去重合并

        兼容标准导出格式与仅含 entities/relations 数组的简化格式。
        返回导入报告（新增/合并数量、类型冲突、错误明细）。
        """
        report = {
            'entities_added': 0,
            'entities_merged': 0,
            'relations_added': 0,
            'relations_merged': 0,
            'conflicts': [],
            'errors': []
        }

        if not isinstance(data, dict):
            report['errors'].append('导入数据必须是JSON对象')
            return report

        raw_entities = data.get('entities', [])
        raw_relations = data.get('relations', [])

        if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
            report['errors'].append('entities 和 relations 字段必须是数组')
            return report

        if not raw_entities and not raw_relations:
            report['errors'].append('导入数据为空：未找到实体或关系')
            return report

        # 1. 导入实体
        entity_types = {}  # 实体文本 -> 规范类型，用于补全关系缺失的类型
        for index, item in enumerate(raw_entities):
            entity = self._parse_entity(item, index, report)
            if not entity:
                continue

            result = self.storage.merge_entity(
                entity['text'], entity['type'], entity['properties'], entity['count']
            )

            if result == 'added':
                report['entities_added'] += 1
            else:
                report['entities_merged'] += 1
                if result == 'conflict':
                    existing = self.storage.get_entity(entity['text'])
                    report['conflicts'].append({
                        'text': entity['text'],
                        'imported_type': entity['type'],
                        'existing_type': existing['type'] if existing else entity['type']
                    })

            # 记录实体最终归属的类型
            canonical = self.storage.get_entity(entity['text'])
            entity_types[entity['text']] = canonical['type'] if canonical else entity['type']

        # 2. 导入关系
        for index, item in enumerate(raw_relations):
            relation = self._parse_relation(item, index, entity_types, report)
            if not relation:
                continue

            result = self.storage.merge_relation(
                relation['subject'], relation['subject_type'],
                relation['predicate'],
                relation['object'], relation['object_type'],
                relation['properties']
            )

            if result == 'added':
                report['relations_added'] += 1
            else:
                report['relations_merged'] += 1

        report['statistics'] = self.storage.get_statistics()
        return report

    def _parse_entity(self, item, index: int, report: Dict):
        """校验并规范化单条实体数据"""
        if not isinstance(item, dict):
            report['errors'].append(f'实体[{index}]: 数据格式错误，必须是对象')
            return None

        text = item.get('text')
        if not isinstance(text, str) or not text.strip():
            report['errors'].append(f'实体[{index}]: 缺少有效的 text 字段')
            return None

        entity_type = item.get('type', 'OTHER')
        if entity_type not in ENTITY_TYPES:
            entity_type = 'OTHER'

        properties = item.get('properties', {})
        if not isinstance(properties, dict):
            properties = {}

        count = item.get('count', 1)
        if not isinstance(count, int) or count < 1:
            count = 1

        return {
            'text': text.strip(),
            'type': entity_type,
            'properties': properties,
            'count': count
        }

    def _parse_relation(self, item, index: int, entity_types: Dict, report: Dict):
        """校验并规范化单条关系数据"""
        if not isinstance(item, dict):
            report['errors'].append(f'关系[{index}]: 数据格式错误，必须是对象')
            return None

        for field in ('subject', 'predicate', 'object'):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                report['errors'].append(f'关系[{index}]: 缺少有效的 {field} 字段')
                return None

        subject = item['subject'].strip()
        obj = item['object'].strip()

        # 类型缺失时依次从导入实体、现有图谱推断，兜底 OTHER
        subject_type = self._resolve_type(item.get('subject_type'), subject, entity_types)
        object_type = self._resolve_type(item.get('object_type'), obj, entity_types)

        properties = item.get('properties', {})
        if not isinstance(properties, dict):
            properties = {}

        return {
            'subject': subject,
            'subject_type': subject_type,
            'predicate': item['predicate'].strip(),
            'object': obj,
            'object_type': object_type,
            'properties': properties
        }

    def _resolve_type(self, declared_type, entity_text: str, entity_types: Dict) -> str:
        """推断实体类型：声明值 -> 导入实体 -> 现有图谱 -> OTHER"""
        if declared_type in ENTITY_TYPES:
            return declared_type
        if entity_text in entity_types:
            return entity_types[entity_text]
        existing = self.storage.get_entity(entity_text)
        if existing:
            return existing['type']
        return 'OTHER'
