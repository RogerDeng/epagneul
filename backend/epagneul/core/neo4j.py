from datetime import datetime
from uuid import uuid4


from epagneul import settings
from epagneul.models.relationships import RelationshipInDB
from epagneul.models.files import File
from epagneul.models.folders import Folder, FolderInDB
from epagneul.models.graph import Edge, Node
from epagneul.models.observables import Observable, ObservableType
from neo4j import GraphDatabase


def chunker(seq, size):
    return (seq[pos : pos + size] for pos in range(0, len(seq), size))


class DataBase:
    def __init__(self):
        self._driver = GraphDatabase.driver(
            settings.neo4j.endpoint,
            auth=(settings.neo4j.username, settings.neo4j.password),
        )

    def bootstrap(self):
        print("bootstrap db")
        """
        with self._driver.session() as session:
            session.run("CREATE CONSTRAINT machine_id ON (n:Machine) ASSERT n.identifier IS UNIQUE")
            session.run("CREATE CONSTRAINT user_id ON (n:User) ASSERT n.identifier IS UNIQUE")
        """

    def close(self):
        self._driver.close()
        print("close db")

    def rm(self):
        with self._driver.session() as session:
            session.run("MATCH (n) " "DETACH DELETE n")

    def get_graph(self, folder: str):
        nodes = {}
        edges = []

        def _get_or_add_node(node):
            if node["id"] in nodes:
                return nodes[node["id"]].data.id

            new_node = Node(data=Observable(**node))
            new_node.data.width = 10 + (len(new_node.data.label) * 11)
            compound_id = f"Compound-{node['algo_lpa']}"
            if compound_id not in nodes:
                nodes[compound_id] = Node(
                    data=Observable(
                        id=compound_id,
                        category=ObservableType.COMPOUND,
                        border_width=1,
                        border_color="#222023"
                    )
                )
            new_node.data.parent = compound_id
            nodes[node["id"]] = new_node

            return new_node.data.id

        with self._driver.session() as session:
            res = session.run(
                "MATCH (source {folder: $folder})-[rel:LogonEvent]->(target {folder: $folder}) "
                "return source, PROPERTIES(rel) as rel, target",
                folder=folder,
            )
            for item in res:
                source_id = _get_or_add_node(item["source"])
                target_id = _get_or_add_node(item["target"])
                rel = item["rel"]
                rel["source"] = source_id
                rel["target"] = target_id
                rel["tip"] = rel["tip"] + f"<br>Count: {rel['count']}"
                edges.append(Edge(data=RelationshipInDB(**item["rel"], id=uuid4().hex)))

        return list(nodes.values()), edges

    def create_folder(self, folder: Folder):
        with self._driver.session() as session:
            session.run(
                "CREATE (folder: Folder) SET folder += $data", data=folder.dict()
            )

    def get_folders(self):
        with self._driver.session() as session:
            result = session.run("MATCH (folder: Folder) return folder")
            return [Folder(**folder["folder"]) for folder in result.data()]

    def get_folder(self, folder_id):
        with self._driver.session() as session:
            folder = session.run(
                "MATCH (folder: Folder {identifier: $folder_identifier}) return folder",
                folder_identifier=folder_id,
            )
            files = session.run(
                "MATCH (file: File)-[r]->(folder: Folder {identifier: $folder_identifier}) return collect(file)",
                folder_identifier=folder_id,
            )
            folder_data = folder.single()
            if not folder_data:
                print(f"FOLDER NOT FOUND {folder_id}")
                return None

            files_documents = []
            start_time = end_time = None
            for f in files.single().data()["collect(file)"]:
                file_document = File(**f)
                if not start_time or file_document.start_time < start_time:
                    start_time = file_document.start_time
                if not end_time or file_document.end_time > end_time:
                    end_time = file_document.end_time
                files_documents.append(file_document)

            return FolderInDB(
                **folder_data.data()["folder"],
                start_time=start_time,
                end_time=end_time,
                files=files_documents,
            )

    def remove_folder(self, folder_id):
        with self._driver.session() as session:
            session.run(
                "MATCH (folders: Folder {identifier: $identifier}) DETACH DELETE folders",
                identifier=folder_id,
            )
            session.run(
                "MATCH (files: File {identifier: $identifier}) DETACH DELETE files",
                identifier=folder_id,
            )
            session.run(
                "MATCH (nodes {folder: $identifier}) DETACH DELETE nodes",
                identifier=folder_id,
            )

    def add_folder_file(self, folder_id, file: File):
        with self._driver.session() as session:
            session.run(
                "MATCH (folder: Folder {identifier: $folder_identifier}) "
                "CREATE (file: File) "
                "SET file += $data "
                "CREATE (file)-[:DEPENDS]->(folder) ",
                data=file.dict(),
                folder_identifier=folder_id,
            )

    def add_evtx_store(self, store, folder: str):
        # timeline, detectn, cfdetect = store.get_change_finder()

        groups = []
        for g in store.groups.values():
            g.finalize()
            groups.append(g.dict())

        users = []
        for u in store.users.values():
            u.finalize()
            users.append(u.dict())

        machines = []
        for m in store.machines.values():
            m.finalize()
            machine_dict = m.dict()
            machine_dict["ips"] = list(m.ips)
            machines.append(machine_dict)

        events = []
        for e in store.relationships.values():
            event = RelationshipInDB(
                **e.dict(exclude={"timestamps", "source_type", "target_type"}),
                timestamps=[int(round(datetime.timestamp(ts))) for ts in e.timestamps],
                tip="<br>".join(
                    [
                        f"{k}: {v}"
                        for k, v in e.dict(
                            exclude={"source", "target", "timestamps", "count", "source_type", "target_type"}
                        ).items()
                    ]
                ),
            ).dict()
            event["timestamps"] = list(event["timestamps"])
            events.append(event)

        with self._driver.session() as session:
            print("Adding groups")
            session.run(
                "UNWIND $groups as row "
                "MERGE (group: Group {folder: $folder, id: row.id}) "
                "ON CREATE SET group += row ",
                groups=groups,
                folder=folder,
            )
            print("Adding users")
            session.run(
                "UNWIND $users as row "
                "MERGE (user: User {folder: $folder, id: row.id}) "
                "ON CREATE SET user += row ",
                users=users,
                folder=folder,
            )
            print("Adding machines")
            session.run(
                "UNWIND $machines as row "
                "MERGE (machine: Machine {folder: $folder, id: row.id}) "
                "ON CREATE SET machine += row",
                machines=machines,
                folder=folder,
            )
            print("Adding events")
            for chunk in chunker(events, 10000):
                session.run(
                    "UNWIND $events as row "
                    "MATCH (source {id: row.source, folder: $folder}), (target {id: row.target, folder: $folder}) "
                    "MERGE (source)-[rel: LogonEvent {event_type: row.event_type}]->(target) "
                    "ON CREATE SET rel += row "
                    "ON MATCH SET rel.count = rel.count + row.count",
                    events=chunk,
                    folder=folder,
                )

    def make_lpa(self, folder: str):
        with self._driver.session() as session:
            query = f"""CALL gds.labelPropagation.write({{
                    nodeQuery: 'MATCH (u {{ folder: "{folder}" }}) RETURN id(u) AS id',
                    relationshipQuery: 'MATCH (n {{ folder: "{folder}" }})-[r: LogonEvent]-(m {{ folder: "{folder}" }}) RETURN id(n) AS source, id(m) AS target, type(r) as type',
                    writeProperty: 'algo_lpa'
                }})
            """
            session.run(query)

    def make_pagerank(self, folder: str):
        with self._driver.session() as session:
            query = f"""CALL gds.pageRank.write({{
                    nodeQuery: 'MATCH (u {{ folder: "{folder}" }}) RETURN id(u) AS id',
                    relationshipQuery: 'MATCH (n: User {{ folder: "{folder}" }})-[r: LogonEvent]-(m: Machine {{ folder: "{folder}" }}) RETURN id(n) AS source, id(m) AS target, type(r) as type',
                    dampingFactor: 0.85,
                    writeProperty: 'algo_pagerank'
                }})
            """
            session.run(query)


db = DataBase()


def get_database():
    return db
