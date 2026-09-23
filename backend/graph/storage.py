"""
图谱存储模块 - JSON文件分片存储
"""
import json
import os
import threading
from typing import Dict, List, Optional
from backend.utils.config import GRAPH_DIR, GRAPH_SHARDS


class GraphStorage:
    """图谱存储管理器 - 按实体类型分片"""

    def __init__(self):
        # 使用可重入锁：add_relation 等方法内部会嵌套调用 add_entity
        self.lock = threading.RLock()
        self._ensure_directories()
        self._cache = {}
        self._load_all_shards()

    def _ensure_directories(self):
        """确保目录存在"""
        os.makedirs(GRAPH_DIR, exist_ok=True)

    def _load_all_shards(self):
        """加载所有分片到缓存"""
        for entity_type, filename in GRAPH_SHARDS.items():
            filepath = os.path.join(GRAPH_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    self._cache[entity_type] = json.load(f)
            else:
                self._cache[entity_type] = {'entities': {}, 'relations': []}

    def _save_shard(self, entity_type: str):
        """保存指定分片到文件"""
        filename = GRAPH_SHARDS.get(entity_type, 'other.json')
        filepath = os.path.join(GRAPH_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._cache[entity_type], f, ensure_ascii=False, indent=2)

    def add_entity(self, entity_text: str, entity_type: str, properties: Dict = None):
        """添加实体"""
        with self.lock:
            if entity_type not in self._cache:
                self._cache[entity_type] = {'entities': {}, 'relations': []}

            if entity_text not in self._cache[entity_type]['entities']:
                self._cache[entity_type]['entities'][entity_text] = {
                    'id': f"{entity_type}_{len(self._cache[entity_type]['entities'])}",
                    'text': entity_text,
                    'type': entity_type,
                    'properties': properties or {},
                    'count': 1
                }
            else:
                self._cache[entity_type]['entities'][entity_text]['count'] += 1

            self._save_shard(entity_type)

    def add_relation(self, subject: str, subject_type: str, predicate: str,
                     obj: str, object_type: str, properties: Dict = None):
        """添加关系"""
        with self.lock:
            # 确保实体存在
            self.add_entity(subject, subject_type)
            self.add_entity(obj, object_type)

            # 添加关系到主语所在分片
            if subject_type not in self._cache:
                self._cache[subject_type] = {'entities': {}, 'relations': []}

            relation = {
                'subject': subject,
                'subject_type': subject_type,
                'predicate': predicate,
                'object': obj,
                'object_type': object_type,
                'properties': properties or {}
            }

            # 检查是否已存在
            existing = self._cache[subject_type]['relations']
            if not any(r['subject'] == subject and r['predicate'] == predicate and r['object'] == obj for r in existing):
                existing.append(relation)
                self._save_shard(subject_type)

    def merge_entity(self, entity_text: str, entity_type: str,
                     properties: Dict = None, count: int = 1) -> str:
        """合并实体（用于数据导入）

        按 文本+类型 去重：已存在则合并属性并累加出现次数；
        同名实体已存在于其他类型时合并到已有实体，返回 'conflict'。

        Returns:
            'added' | 'merged' | 'conflict'
        """
        with self.lock:
            # 同名实体存在于其他类型分片 -> 合并到已有实体，避免重复节点
            existing_entity = self.get_entity(entity_text)
            if existing_entity and existing_entity['type'] != entity_type:
                self._merge_entity_properties(existing_entity, properties, count)
                self._save_shard(existing_entity['type'])
                return 'conflict'

            if entity_type not in self._cache:
                self._cache[entity_type] = {'entities': {}, 'relations': []}

            shard = self._cache[entity_type]
            if entity_text in shard['entities']:
                self._merge_entity_properties(shard['entities'][entity_text], properties, count)
                self._save_shard(entity_type)
                return 'merged'

            shard['entities'][entity_text] = {
                'id': f"{entity_type}_{len(shard['entities'])}",
                'text': entity_text,
                'type': entity_type,
                'properties': properties or {},
                'count': max(1, count)
            }
            self._save_shard(entity_type)
            return 'added'

    def _merge_entity_properties(self, entity: Dict, properties: Dict, count: int):
        """合并实体属性与出现次数：已有属性优先，补充新属性"""
        for key, value in (properties or {}).items():
            entity.setdefault('properties', {}).setdefault(key, value)
        entity['count'] = entity.get('count', 1) + max(0, count)

    def merge_relation(self, subject: str, subject_type: str, predicate: str,
                       obj: str, object_type: str, properties: Dict = None) -> str:
        """合并关系（用于数据导入）

        按 (主语, 谓语, 宾语) 在所有分片中全局去重，重复时合并属性。

        Returns:
            'added' | 'merged'
        """
        with self.lock:
            # 确保实体存在（count=0：仅兜底创建，不影响已有实体的出现次数），
            # 并获取实体的规范类型（可能因冲突合并到其他类型）
            self.merge_entity(subject, subject_type, count=0)
            self.merge_entity(obj, object_type, count=0)
            subject_entity = self.get_entity(subject)
            object_entity = self.get_entity(obj)
            canonical_subject_type = subject_entity['type'] if subject_entity else subject_type
            canonical_object_type = object_entity['type'] if object_entity else object_type

            # 跨所有分片查重
            existing_relation = self._find_relation(subject, predicate, obj)
            if existing_relation:
                for key, value in (properties or {}).items():
                    existing_relation.setdefault('properties', {}).setdefault(key, value)
                self._save_shard(existing_relation['subject_type'])
                return 'merged'

            if canonical_subject_type not in self._cache:
                self._cache[canonical_subject_type] = {'entities': {}, 'relations': []}

            self._cache[canonical_subject_type]['relations'].append({
                'subject': subject,
                'subject_type': canonical_subject_type,
                'predicate': predicate,
                'object': obj,
                'object_type': canonical_object_type,
                'properties': properties or {}
            })
            self._save_shard(canonical_subject_type)
            return 'added'

    def _find_relation(self, subject: str, predicate: str, obj: str) -> Optional[Dict]:
        """在所有分片中查找关系"""
        for shard in self._cache.values():
            for relation in shard['relations']:
                if (relation['subject'] == subject and relation['predicate'] == predicate
                        and relation['object'] == obj):
                    return relation
        return None

    def get_entity(self, entity_text: str) -> Optional[Dict]:
        """获取实体信息"""
        for entity_type, shard in self._cache.items():
            if entity_text in shard['entities']:
                return shard['entities'][entity_text]
        return None

    def get_entity_relations(self, entity_text: str) -> List[Dict]:
        """获取实体的所有关系"""
        relations = []
        for entity_type, shard in self._cache.items():
            for relation in shard['relations']:
                if relation['subject'] == entity_text or relation['object'] == entity_text:
                    relations.append(relation)
        return relations

    def get_all_entities(self) -> List[Dict]:
        """获取所有实体"""
        entities = []
        for entity_type, shard in self._cache.items():
            entities.extend(shard['entities'].values())
        return entities

    def get_all_relations(self) -> List[Dict]:
        """获取所有关系"""
        relations = []
        for entity_type, shard in self._cache.items():
            relations.extend(shard['relations'])
        return relations

    def get_graph_data(self) -> Dict:
        """获取图谱可视化数据"""
        nodes = []
        links = []
        node_ids = set()

        for entity_type, shard in self._cache.items():
            for entity_text, entity_data in shard['entities'].items():
                if entity_data['id'] not in node_ids:
                    node_ids.add(entity_data['id'])
                    nodes.append({
                        'id': entity_data['id'],
                        'label': entity_text,
                        'type': entity_type,
                        'count': entity_data.get('count', 1)
                    })

            for relation in shard['relations']:
                source_entity = self.get_entity(relation['subject'])
                target_entity = self.get_entity(relation['object'])
                if source_entity and target_entity:
                    links.append({
                        'source': source_entity['id'],
                        'target': target_entity['id'],
                        'label': relation['predicate']
                    })

        return {'nodes': nodes, 'links': links}

    def search_entities(self, keyword: str) -> List[Dict]:
        """搜索实体"""
        results = []
        for entity_type, shard in self._cache.items():
            for entity_text, entity_data in shard['entities'].items():
                if keyword in entity_text:
                    results.append(entity_data)
        return results

    def get_statistics(self) -> Dict:
        """获取图谱统计信息"""
        total_entities = 0
        total_relations = 0
        entity_counts = {}

        for entity_type, shard in self._cache.items():
            count = len(shard['entities'])
            entity_counts[entity_type] = count
            total_entities += count
            total_relations += len(shard['relations'])

        return {
            'total_entities': total_entities,
            'total_relations': total_relations,
            'entity_counts': entity_counts
        }
